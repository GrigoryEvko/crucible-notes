# Math Intrinsics

> *All addresses in this page apply to ptxas v13.0.88 (CUDA 13.0). Other versions will differ.*

ptxas provides built-in IEEE-compliant math functions for operations that have no single-instruction hardware implementation. When a PTX instruction like `div.rn.f64`, `rcp.rn.f32`, `sqrt.rn.f64`, or `rsqrt.approx.f64` is encountered, ptxas either emits a single MUFU (Multi-Function Unit) instruction for approximate results, or generates a multi-instruction SASS sequence using Newton-Raphson refinement for IEEE-compliant precision. For operations too complex to inline, ptxas emits a call to one of 70 registered `__cuda_sm20_*` helper functions.

| | |
|---|---|
| **Intrinsic ID range** | `0x3D`--`0x86` (70 math entries + 4 sm_3x division variants) |
| **Math codegen handlers** | 9 functions: `div.full`, `div`, `rem`, `rcp`, `rsqrt`, `sqrt`, `ex2`, `lg2`, `tanh` |
| **Newton-Raphson templates** | 4 top-level handlers at `0x1700000`--`0x172A090` (~180 KB) |
| **MUFU internal opcode** | `0x3C` (60 decimal), Ori mnemonic `ZHSH` |
| **MUFU Mercury major** | `0x58`, minor sub-function encoded in operand fields |
| **SFU functional unit** | Index 8 in the latency model (RCP, RSQ, SIN, COS, EX2, LG2) |
| **MUFU encoding variants** | 14 (reg/reg, reg/pred, reg/ureg, bar operands) |

## MUFU -- Multi-Function Unit

The MUFU instruction is a single-cycle-issue instruction that computes transcendental approximations on the SFU (Special Function Unit). Each SM has a dedicated SFU pipe that executes MUFU operations independently of the ALU pipes.

### Sub-Function Table

The MUFU sub-function is encoded in the instruction's modifier field (not a separate operand). The following sub-functions are available across all SM architectures supported by ptxas v13.0:

| Sub-Function | Operation | Input | Output | Precision |
|---|---|---|---|---|
| `MUFU.COS` | cos(x * 2pi) | FP32 | FP32 | ~22 bits mantissa |
| `MUFU.SIN` | sin(x * 2pi) | FP32 | FP32 | ~22 bits mantissa |
| `MUFU.EX2` | 2^x | FP32 | FP32 | ~22 bits mantissa |
| `MUFU.LG2` | log2(x) | FP32 | FP32 | ~22 bits mantissa |
| `MUFU.RCP` | 1/x | FP32 | FP32 | ~23 bits mantissa |
| `MUFU.RSQ` | 1/sqrt(x) | FP32 | FP32 | ~23 bits mantissa |
| `MUFU.RCP64H` | 1/x (FP64 high-word seed) | FP32 | FP32 | ~23 bits, sm_80+ |
| `MUFU.RSQ64H` | 1/sqrt(x) (FP64 high-word seed) | FP32 | FP32 | ~23 bits, sm_80+ |
| `MUFU.TANH` | tanh(x) | FP32 | FP32 | ~22 bits, sm_75+ |

MUFU.RCP and MUFU.RSQ produce results accurate to approximately 1 ULP of the true FP32 value (23 mantissa bits). The trigonometric and exponential sub-functions (SIN, COS, EX2, LG2) are slightly less precise at approximately 22 bits. MUFU.TANH was added in Turing (sm_75).

### MUFU in the Ori IR

In the ptxas internal representation, MUFU uses Ori opcode `0x3C` (decimal 60) with the mnemonic `ZHSH`. During instruction selection, PTX operations like `sin.approx.f32`, `cos.approx.f32`, `ex2.approx.f32`, `lg2.approx.f32`, `rcp.approx.f32`, and `rsqrt.approx.f32` are each lowered to a single `ZHSH` (MUFU) instruction with the appropriate sub-function selector.

The lowering pass responsible for MUFU emission is at `sub_80E9B0` (`LowerSpecialFunctions`), called from the master lowering dispatcher `sub_8380A0`. It converts Ori-level special function opcodes into MUFU SASS instructions with appropriate sub-function encoding.

### MUFU Encoding (sm_100+)

In the Mercury/Blackwell encoding, MUFU is major opcode `0x58` with a single variant at the basic encoding level (`sub_10C0170`). The encoding signature:

| Field | Value |
|---|---|
| Major opcode | `0x58` |
| f19 | `0xB` |
| Format | 1 (single-operand class) |
| Operand count | 1 (destination implicit, source = register) |
| Encoding function | `sub_10C0170` |

The variant table at `0xF7CEB0`--`0xF80760` defines 14 encoding patterns for MUFU, supporting combinations of:
- `reg2, reg2` -- standard register source and destination
- `reg2, pred3` -- predicated source
- `reg2, reg10` -- extended register class
- `reg2, ureg4` -- uniform register source (sm_100+ addition)
- `reg2, bar6` -- barrier operand (scheduling)

Uniform register support (`ureg4`) in MUFU is a Blackwell-specific addition, allowing MUFU to consume values directly from the uniform register file without a prior `UMOV` to a general-purpose register.

### Pre-Assignment Constraints

The register allocator applies pre-assignment constraints for MUFU at `sub_93F000`. MUFU (internal opcode 22 in the constraint check, mapped from Ori opcode `0x3C`) requires its operands in specific register classes. The constraint handler calls `sub_93E9D0` with constraint type 1 (early) for MUFU operands.

## Precision Levels

ptxas implements two distinct precision tiers for every math operation, selected by PTX instruction modifiers:

### Approximate (`.approx`)

A single MUFU instruction. This is the default for `sin.approx.f32`, `cos.approx.f32`, `ex2.approx.f32`, `lg2.approx.f32`, `rcp.approx.f32`, and `rsqrt.approx.f32`. The MUFU hardware provides approximately 22--23 bits of mantissa precision in a single instruction dispatch on the SFU pipe.

**PTX to SASS mapping (approximate):**

| PTX Instruction | SASS | Latency |
|---|---|---|
| `sin.approx.f32` | `MUFU.SIN` | SFU pipe, ~4 cycles |
| `cos.approx.f32` | `MUFU.COS` | SFU pipe, ~4 cycles |
| `ex2.approx.f32` | `MUFU.EX2` | SFU pipe, ~4 cycles |
| `lg2.approx.f32` | `MUFU.LG2` | SFU pipe, ~4 cycles |
| `rcp.approx.f32` | `MUFU.RCP` | SFU pipe, ~4 cycles |
| `rsqrt.approx.f32` | `MUFU.RSQ` | SFU pipe, ~4 cycles |
| `tanh.approx.f32` | `MUFU.TANH` | SFU pipe, ~4 cycles (sm_75+) |

### IEEE-Compliant (`.rn`, `.rd`, `.ru`, `.rz`)

Multi-instruction sequences that use MUFU as a seed and refine with Newton-Raphson iterations using FMA instructions. These produce results that are correctly rounded to the specified IEEE 754 rounding mode (round-to-nearest-even, round-down, round-up, round-toward-zero). The instruction count ranges from ~15 for FP32 operations to ~120 for FP64 operations.

The IEEE-compliant paths are implemented in two ways:
1. **Inline templates** -- Multi-instruction SASS sequences emitted directly at the call site by the Newton-Raphson template subsystem (`0x1700000`--`0x172A090`). Used for FP64 division, reciprocal, sqrt, and rsqrt.
2. **Callable helpers** -- Calls to `__cuda_sm20_*` functions whose bodies are pre-compiled PTX routines linked from libdevice. Used for FP32 operations with directed rounding modes and all slowpath variants.

## PTX Math Codegen Handlers

When `sub_5D4190` builds the opcode dispatch table, it registers 9 math-related PTX instruction names to codegen handler functions. Each handler allocates a 50,000-byte temporary buffer, queries instruction properties through accessor functions on the instruction object at `a1+1096`, and generates inline PTX code via sequential `sprintf()` calls.

| PTX Opcode | Handler | Size | Description |
|---|---|---|---|
| `div.full` | `sub_573860` | ~7 KB | FP64 full-precision division (calls Newton-Raphson template) |
| `div` | `sub_5B76D0` | 64 KB | General division: dispatches by type (s16/u16/s64/u64/f32/f64) and rounding mode |
| `rem` | `sub_589810` | ~13 KB | Integer remainder (s16/u16/s64/u64) |
| `rcp` | `sub_5B0CD0` | 44 KB | Reciprocal: dispatches by type (f32/f64) and rounding mode |
| `rsqrt` | `sub_57BFC0` | ~10 KB | Reciprocal square root |
| `sqrt` | `sub_5B4040` | 49 KB | Square root: dispatches by type (f32/f64) and rounding mode |
| `ex2` | `sub_583190` | ~14 KB | Base-2 exponential |
| `lg2` | `sub_52A5C0` | ~5 KB | Base-2 logarithm |
| `tanh` | `sub_505B00` | ~5 KB | Hyperbolic tangent |

### Handler Dispatch Logic

All math codegen handlers follow the same structural pattern. The `div` handler (`sub_5B76D0`, 1,466 lines decompiled, 64 KB) is the largest because it covers the most type/rounding/precision combinations. The handler:

1. Allocates a 50,000-byte output buffer via `sub_424070`
2. Queries the operand type via `sub_70CA60(*(a1+1096), 0)`:
   - Type 58 = FP32 (`f32`)
   - Type 59 = FP64 (`f64`)
   - Type 54 = signed 16-bit (`s16`)
   - Type 56 = unsigned 16-bit / other integer
3. Queries rounding mode, FTZ flag, and precision modifier via additional accessors (`sub_707BC0`, `sub_70B820`, `sub_70B8E0`, `sub_70B710`)
4. Selects the appropriate intrinsic function name and emits a PTX call via `sprintf()` into the output buffer
5. Copies the result to a final allocation and frees the temporary buffer

For the `tanh` handler (`sub_505B00`, 121 lines), the dispatch is simpler:
- Type 56: emits a short-form call to a hardware-supported path
- Type 54: emits a multi-operand call querying register info via `sub_70B8E0` and `sub_70B710`
- Default: emits a generic call with 6 operand parameters

### Type Codes in Math Handlers

The operand type returned by `sub_70CA60(instr, 0)` maps to PTX data types:

| Code | PTX Type | Used By |
|---|---|---|
| 54 | `.s16` / `.s32` | Integer div, rem |
| 56 | `.u16` / `.u32` | Integer div, rem, tanh variant |
| 58 | `.f32` | Float div, rcp, sqrt, rsqrt, ex2, lg2, tanh |
| 59 | `.f64` | Double div, rcp, sqrt, rsqrt |

## Registered Math Intrinsics (IDs `0x3D`--`0x86`)

The master registration function `sub_5D1660` registers 70 math helper functions with IDs `0x3D` through `0x82`, plus 4 sm_3x-optimized division variants at `0x83`--`0x86`. These are the `__cuda_sm20_*` functions whose PTX prototypes are emitted by the prototype generator `sub_5FF700`.

### Division Intrinsics (22 entries)

| ID | Name | Category |
|---|---|---|
| `0x41` | `__cuda_sm20_div_rd_f32` | FP32 div, round-down |
| `0x42` | `__cuda_sm20_div_rd_f64_v2` | FP64 div, round-down |
| `0x43` | `__cuda_sm20_div_rd_ftz_f32` | FP32 div, round-down, flush-to-zero |
| `0x44` | `__cuda_sm20_div_rn_f32` | FP32 div, round-to-nearest |
| `0x45` | `__cuda_sm20_div_rn_f64_fast` | FP64 div, round-to-nearest (fast path) |
| `0x46` | `__cuda_sm20_div_rn_f64_full` | FP64 div, round-to-nearest (full IEEE) |
| `0x47` | `__cuda_sm20_div_rn_ftz_f32` | FP32 div, round-to-nearest, FTZ |
| `0x48` | `__cuda_sm20_div_rn_ftz_f32_slowpath` | FP32 div RN FTZ (denormal handler) |
| `0x49` | `__cuda_sm20_div_rn_noftz_f32_slowpath` | FP32 div RN no-FTZ (denormal handler) |
| `0x4A` | `__cuda_sm20_div_ru_f32` | FP32 div, round-up |
| `0x4B` | `__cuda_sm20_div_ru_f64_v2` | FP64 div, round-up |
| `0x4C` | `__cuda_sm20_div_ru_ftz_f32` | FP32 div, round-up, FTZ |
| `0x4D` | `__cuda_sm20_div_rz_f32` | FP32 div, round-toward-zero |
| `0x4E` | `__cuda_sm20_div_rz_f64_v2` | FP64 div, round-toward-zero |
| `0x4F` | `__cuda_sm20_div_rz_ftz_f32` | FP32 div, round-toward-zero, FTZ |
| `0x50` | `__cuda_sm20_div_s16` | Signed 16-bit integer div |
| `0x51` | `__cuda_sm20_div_s64` | Signed 64-bit integer div |
| `0x52` | `__cuda_sm20_div_u16` | Unsigned 16-bit integer div |
| `0x53` | `__cuda_sm20_div_u64` | Unsigned 64-bit integer div |
| `0x83` | `__cuda_sm3x_div_rn_ftz_f32` | sm_30+ optimized FP32 div RN FTZ |
| `0x84` | `__cuda_sm3x_div_rn_ftz_f32_slowpath` | sm_30+ FP32 div RN FTZ slowpath |
| `0x85` | `__cuda_sm3x_div_rn_noftz_f32` | sm_30+ optimized FP32 div RN |
| `0x86` | `__cuda_sm3x_div_rn_noftz_f32_slowpath` | sm_30+ FP32 div RN slowpath |

### Reciprocal Intrinsics (20 entries)

| ID | Name | Category |
|---|---|---|
| `0x40` | `__cuda_sm20_dblrcp_rn_slowpath_v3` | FP64 reciprocal slowpath |
| `0x5B` | `__cuda_sm20_rcp_f64_v3` | FP64 reciprocal (default rounding) |
| `0x5C` | `__cuda_sm20_rcp_rd_f32` | FP32 rcp, round-down |
| `0x5D` | `__cuda_sm20_rcp_rd_f32_slowpath` | FP32 rcp RD slowpath |
| `0x5E` | `__cuda_sm20_rcp_rd_f64` | FP64 rcp, round-down |
| `0x5F` | `__cuda_sm20_rcp_rd_ftz_f32` | FP32 rcp RD FTZ |
| `0x60` | `__cuda_sm20_rcp_rd_ftz_f32_slowpath` | FP32 rcp RD FTZ slowpath |
| `0x61` | `__cuda_sm20_rcp_rn_f32` | FP32 rcp, round-to-nearest |
| `0x62` | `__cuda_sm20_rcp_rn_f32_slowpath` | FP32 rcp RN slowpath |
| `0x63` | `__cuda_sm20_rcp_rn_ftz_f32` | FP32 rcp RN FTZ |
| `0x64` | `__cuda_sm20_rcp_rn_ftz_f32_slowpath` | FP32 rcp RN FTZ slowpath |
| `0x65` | `__cuda_sm20_rcp_ru_f32` | FP32 rcp, round-up |
| `0x66` | `__cuda_sm20_rcp_ru_f32_slowpath` | FP32 rcp RU slowpath |
| `0x67` | `__cuda_sm20_rcp_ru_f64` | FP64 rcp, round-up |
| `0x68` | `__cuda_sm20_rcp_ru_ftz_f32` | FP32 rcp RU FTZ |
| `0x69` | `__cuda_sm20_rcp_ru_ftz_f32_slowpath` | FP32 rcp RU FTZ slowpath |
| `0x6A` | `__cuda_sm20_rcp_rz_f32` | FP32 rcp, round-toward-zero |
| `0x6B` | `__cuda_sm20_rcp_rz_f32_slowpath` | FP32 rcp RZ slowpath |
| `0x6C` | `__cuda_sm20_rcp_rz_f64` | FP64 rcp, round-toward-zero |
| `0x6D` | `__cuda_sm20_rcp_rz_ftz_f32` | FP32 rcp RZ FTZ |
| `0x6E` | `__cuda_sm20_rcp_rz_ftz_f32_slowpath` | FP32 rcp RZ FTZ slowpath |

### Square Root Intrinsics (17 entries)

| ID | Name | Category |
|---|---|---|
| `0x56` | `__cuda_sm20_dsqrt_rd_f64` | FP64 sqrt, round-down |
| `0x57` | `__cuda_sm20_dsqrt_rn_f64_mediumpath_v1` | FP64 sqrt RN (medium-complexity path) |
| `0x58` | `__cuda_sm20_dsqrt_rn_f64_v3` | FP64 sqrt, round-to-nearest |
| `0x59` | `__cuda_sm20_dsqrt_ru_f64` | FP64 sqrt, round-up |
| `0x5A` | `__cuda_sm20_dsqrt_rz_f64` | FP64 sqrt, round-toward-zero |
| `0x73` | `__cuda_sm20_sqrt_rd_f32` | FP32 sqrt, round-down |
| `0x74` | `__cuda_sm20_sqrt_rd_f32_slowpath` | FP32 sqrt RD slowpath |
| `0x75` | `__cuda_sm20_sqrt_rd_ftz_f32` | FP32 sqrt RD FTZ |
| `0x76` | `__cuda_sm20_sqrt_rd_ftz_f32_slowpath` | FP32 sqrt RD FTZ slowpath |
| `0x77` | `__cuda_sm20_sqrt_rn_f32` | FP32 sqrt, round-to-nearest |
| `0x78` | `__cuda_sm20_sqrt_rn_f32_slowpath` | FP32 sqrt RN slowpath |
| `0x79` | `__cuda_sm20_sqrt_rn_ftz_f32` | FP32 sqrt RN FTZ |
| `0x7A` | `__cuda_sm20_sqrt_rn_ftz_f32_slowpath` | FP32 sqrt RN FTZ slowpath |
| `0x7B` | `__cuda_sm20_sqrt_ru_f32` | FP32 sqrt, round-up |
| `0x7C` | `__cuda_sm20_sqrt_ru_f32_slowpath` | FP32 sqrt RU slowpath |
| `0x7D` | `__cuda_sm20_sqrt_ru_ftz_f32` | FP32 sqrt RU FTZ |
| `0x7E` | `__cuda_sm20_sqrt_ru_ftz_f32_slowpath` | FP32 sqrt RU FTZ slowpath |
| `0x7F` | `__cuda_sm20_sqrt_rz_f32` | FP32 sqrt, round-toward-zero |
| `0x80` | `__cuda_sm20_sqrt_rz_f32_slowpath` | FP32 sqrt RZ slowpath |
| `0x81` | `__cuda_sm20_sqrt_rz_ftz_f32` | FP32 sqrt RZ FTZ |
| `0x82` | `__cuda_sm20_sqrt_rz_ftz_f32_slowpath` | FP32 sqrt RZ FTZ slowpath |

### Reciprocal Square Root Intrinsics (2 entries)

| ID | Name | Category |
|---|---|---|
| `0x54` | `__cuda_sm20_drsqrt_f64_slowpath_v2` | FP64 rsqrt slowpath |
| `0x55` | `__cuda_sm20_drsqrt_f64_v2` | FP64 rsqrt default |

### Remainder Intrinsics (4 entries)

| ID | Name | Category |
|---|---|---|
| `0x6F` | `__cuda_sm20_rem_s16` | Signed 16-bit remainder |
| `0x70` | `__cuda_sm20_rem_s64` | Signed 64-bit remainder |
| `0x71` | `__cuda_sm20_rem_u16` | Unsigned 16-bit remainder |
| `0x72` | `__cuda_sm20_rem_u64` | Unsigned 64-bit remainder |

### Bit-Field Intrinsics (3 entries)

| ID | Name | Category |
|---|---|---|
| `0x3D` | `__cuda_sm20_bfe_s64_` | 64-bit signed bit-field extract |
| `0x3E` | `__cuda_sm20_bfe_u64_` | 64-bit unsigned bit-field extract |
| `0x3F` | `__cuda_sm20_bfi_u64_` | 64-bit unsigned bit-field insert |

### Naming Conventions

The intrinsic name encodes the complete variant specification:

```text
__cuda_sm{gen}_{op}_{rounding}_{ftz}_{type}_{suffix}
```

| Component | Values | Meaning |
|---|---|---|
| `sm{gen}` | `sm20`, `sm3x` | Minimum SM architecture |
| `{op}` | `div`, `rcp`, `sqrt`, `dsqrt`, `drsqrt`, `rem`, `bfe`, `bfi` | Mathematical operation |
| `{rounding}` | `rn`, `rd`, `ru`, `rz` | IEEE 754 rounding mode |
| `{ftz}` | `ftz`, `noftz` | Flush-to-zero denormal behavior |
| `{type}` | `f32`, `f64`, `s16`, `s64`, `u16`, `u64` | Operand data type |
| `{suffix}` | `slowpath`, `mediumpath`, `full`, `fast`, `v2`, `v3` | Implementation variant |

The `slowpath` suffix indicates a handler for denormalized inputs or edge cases (NaN, infinity, zero) that the fast path branches around. The `v2`/`v3` suffixes indicate iteration count on the implementation (each version may use different Newton-Raphson step counts or algorithm improvements).

### Prototype Format

The prototype generator `sub_5FF700` (354 KB) emits `.weak .func` PTX declarations for every registered intrinsic. Example prototypes:

```ptx
.weak .func (.reg .s32 %d) __cuda_sm20_div_s16
    (.reg .s32 %a0, .reg .s32 %a1)

.weak .func (.reg .u64 %rdv1) __cuda_sm20_div_u64
    (.reg .u64 %rda1, .reg .u64 %rda2)

.weak .func (.reg .f32 %fv1) __cuda_sm20_div_rn_f32
    (.reg .f32 %fa1, .reg .f32 %fa2)

.weak .func (.reg .f64 %fdv1) __cuda_sm20_div_rn_f64_full
    (.reg .f64 %fda1, .reg .f64 %fda2)
```

The `.weak` linkage allows user-provided implementations to override the built-in versions at link time.

## Newton-Raphson Refinement Templates

For FP64 operations, ptxas emits multi-instruction SASS sequences inline rather than calling helper functions. These sequences are generated by the **template subsystem** at `0x1700000`--`0x172A090` (36 functions, ~180 KB). The templates use MUFU hardware as the initial seed and iterate Newton-Raphson to achieve full FP64 precision. See [Newton-Raphson & Math Templates](../codegen/templates.md) for complete details.

### Template Hierarchy

```text
sub_AED3C0 (Master Lowering Dispatcher, 28 KB)
  |
  +-- sub_170E8B0 (DDIV handler)        -- FP64 division
  |     +-- sub_170E260 (coordinator)    -- 298 vregs, 6 sub-expanders
  |
  +-- sub_1718D60 (DRCP/DSQRT handler)  -- FP64 reciprocal / square root
  |     +-- sub_1718790 (coordinator)    -- 289 vregs, 7 sub-expanders
  |
  +-- sub_17276C0 (DRSQRT handler)      -- FP64 reciprocal square root
  |     +-- sub_1720D60 (coordinator A) -- 247 vregs, 5 sub-expanders
  |     +-- sub_1727130 (coordinator B) -- 59 vregs, integer div/mod path
  |
  +-- sub_1704070 (Inline DDIV handler) -- Register-pressure variants
```

### FP64 Division (DDIV)

Algorithm for `a / b`:

1. Extract the high 32 bits of the FP64 divisor `b`
2. Convert to FP32 and compute `MUFU.RCP` -- ~23-bit seed for `1/b`
3. Newton-Raphson iteration 1: `x1 = x0 * (2 - b * x0)` via DFMA -- ~46 bits
4. Newton-Raphson iteration 2 (partial): guard bits for correct rounding
5. Compute `a * (1/b)` using the refined reciprocal
6. Apply IEEE 754 rounding, handle overflow/underflow/NaN

The complete DDIV template emits ~100--120 SASS instructions across 3 named code sections (`__ori_template_DDIV1`, `__ori_template_DDIV2`, `__ori_template_DDIV3`), using 298 virtual registers. Three register-pressure variants are available:

| Register Limit | Handler | Strategy |
|---|---|---|
| > 20,479 | `sub_1702990` | Full unrolled, maximum ILP |
| > 16,383 | `sub_1701F10` | Partially spilled |
| <= 16,383 | `sub_1701860` | Minimal-register, more instructions |

### FP64 Reciprocal (DRCP)

Algorithm for `1/b`:

1. `MUFU.RCP(float32(b))` -- ~23-bit seed
2. Newton-Raphson iteration 1: `x1 = x0 * (2 - b * x0)` via DFMA
3. Newton-Raphson iteration 2: doubles precision to ~52+ bits
4. Final rounding to FP64 precision

Implemented by `sub_1718D60` (coordinator at `sub_1718790`, 289 vregs, 7 sub-expanders: `sub_170ED40` through `sub_1717470`).

### FP64 Square Root (DSQRT)

Algorithm for `sqrt(a)`:

1. `MUFU.RSQ(float32(a))` -- ~23-bit seed for `1/sqrt(a)`
2. Newton-Raphson refinement: `y1 = y0 * (3 - a * y0^2) / 2`
3. Compute `sqrt(a) = a * (1/sqrt(a))`
4. Apply IEEE 754 rounding

Shares the coordinator with DRCP (`sub_1718790`), selecting the DSQRT sub-expanders (`sub_1715910`, `sub_1717470`) based on the original PTX operation.

### FP64 Reciprocal Square Root (DRSQRT)

The most complex template handler (`sub_17276C0`). Dispatches based on a hardware capability flag at `*(*(ctx+1584)+1037) & 1`:

- **Flag set** (sm_80+ with enhanced SFU): `sub_1727130` -- 59 vregs, fewer refinement iterations due to `MUFU.RSQ64H` providing better initial precision
- **Flag clear** (older architectures): `sub_1720D60` -- 247 vregs, full Newton-Raphson with 5 sub-expanders

### Integer Division via MUFU

Integer division by variable values also uses MUFU.RCP as a starting point. The algorithm for unsigned 32-bit `a / b`:

```text
I2F(b) -> MUFU.RCP -> F2I -> IMAD.HI -> correction
```

Specifically:
1. `float_b = I2F(b)` -- convert divisor to FP32
2. `rcp = MUFU.RCP(float_b)` -- ~23-bit reciprocal approximation
3. `int_rcp = F2I(rcp)` -- convert back to integer
4. `q_est = IMAD.HI(a, int_rcp, 0)` -- estimated quotient
5. `r_est = IMAD(q_est, -b, a)` -- estimated remainder
6. Correction: up to 2 iterations of `if (r_est >= b) q_est++; r_est -= b`

The correction steps (at most 2) are needed because MUFU.RCP is accurate to within 2 ULP. This sequence emits ~50 SASS instructions for 32-bit (`sub_1724A20`, 28 KB decompiled) and ~80 for 64-bit unsigned (`sub_1728930`, 16.5 KB).

## FP32 Math Paths

### Approximate vs Full-Range

For FP32 operations, the codegen handler selects between:

1. **Single MUFU** -- for `.approx` modifier. One instruction, ~23-bit precision.
2. **MUFU + correction** -- for `.rn`/`.rd`/`.ru`/`.rz` with FTZ. MUFU seed plus 1--2 FMA correction steps, inline.
3. **Helper function call** -- for directed rounding modes (RD/RU/RZ) without FTZ, or when denormal handling is required (slowpath variants). Calls to `__cuda_sm20_*` or `__cuda_sm3x_*` functions.

### Flush-to-Zero (FTZ)

The `.ftz` modifier on FP32 operations flushes denormalized inputs and outputs to zero, which simplifies the math sequence:
- Eliminates denormal input handling branches
- Eliminates denormal output rounding logic
- Allows a shorter inline sequence instead of a function call

Each FP32 math intrinsic exists in both FTZ and non-FTZ variants (e.g., `__cuda_sm20_rcp_rn_ftz_f32` vs `__cuda_sm20_rcp_rn_f32`), and many also have a `slowpath` variant for edge cases.

### sm_3x Optimized Division

Four additional division intrinsics at IDs `0x83`--`0x86` provide sm_30+ optimized paths for FP32 round-to-nearest division. The `__cuda_sm3x_div_rn_ftz_f32` and `__cuda_sm3x_div_rn_noftz_f32` variants (plus their slowpath counterparts) take advantage of Kepler+ hardware improvements to produce shorter instruction sequences than the sm_20 versions.

## FP16 Math Handling

FP16 (`half`) math operations do not use MUFU directly. Instead, ptxas:

1. Promotes FP16 inputs to FP32 via `H2F` (half-to-float conversion)
2. Performs the FP32 MUFU operation
3. Converts the result back to FP16 via `F2H` (float-to-half conversion)

For HMMA (half-precision matrix multiply-accumulate) operations, the tensor core path is used instead -- see [Tensor Core Intrinsics](tensor.md).

The `HADD2`, `HMUL2`, `HFMA2` instructions operate on packed FP16x2 values and are separate from the MUFU path. These are direct hardware instructions dispatched to the ALU pipe, not the SFU.

## Codegen Handler Deep Dive

### `sub_5B76D0` -- Division Codegen (64 KB)

The largest math codegen handler at 1,466 decompiled lines. Its dispatch tree:

```text
sub_70CA60(instr, 0) -> operand type
  |
  +-- type 58 (f32)
  |     +-- sub_707BC0(instr) -> rounding mode check
  |     |     +-- mode 1 -> short-form call (approx)
  |     |     +-- mode > 39 -> full Newton-Raphson inline sequence
  |     |     +-- else -> helper function call
  |     +-- sub_70B820(instr) -> precision modifier
  |           +-- <= 39 -> 3-operand compact call
  |           +-- > 39 -> multi-segment inline expansion
  |
  +-- type 59 (f64)
  |     +-- full/fast path selection
  |     +-- rounding mode -> specific __cuda_sm20_div_r{n,d,u,z}_f64 call
  |
  +-- type 54 (s16/s32)
  |     +-- __cuda_sm20_div_s{16,64} call
  |
  +-- type 56 (u16/u32)
        +-- __cuda_sm20_div_u{16,64} call
```

The FP32 path at rounding mode > 39 generates a multi-segment inline PTX sequence with ~20 `sprintf()` calls, each appending a PTX instruction to the output buffer. This is the full-range IEEE-compliant FP32 division path that uses MUFU.RCP as a seed followed by FMA-based correction.

### `sub_5B0CD0` -- Reciprocal Codegen (44 KB)

Similar structure to the division handler. Dispatches by type (f32/f64) and rounding mode. For FP64, calls `__cuda_sm20_rcp_f64_v3`. For FP32, selects between 4 rounding modes x 2 FTZ variants x 2 paths (fast/slowpath) = up to 16 different intrinsic calls.

### `sub_5B4040` -- Square Root Codegen (49 KB)

Handles both FP32 (`__cuda_sm20_sqrt_*`) and FP64 (`__cuda_sm20_dsqrt_*`) variants. For FP64, the `dsqrt_rn_f64_mediumpath_v1` variant provides an intermediate-complexity path between the fast approximation and the full Newton-Raphson template.

### `sub_583190` -- Base-2 Exponential (ex2)

Dispatches by operand type:
- FP32 with mode 1: short-form approximate path (single MUFU.EX2)
- FP32 with rounding mode > 39: full-range inline sequence with ~18 `sprintf()` segments generating a PTX sequence that includes range reduction, MUFU.EX2, and polynomial correction
- FP64: multi-operand call to a helper function

### `sub_57BFC0` -- Reciprocal Square Root (rsqrt)

Dispatches by type:
- FP64 with mode 1: short-form call to `__cuda_sm20_drsqrt_f64_v2`
- FP64 with rounding mode > 39: full inline sequence with ~35 `sprintf()` segments -- the longest inline math expansion for a single-precision-equivalent operation. The sequence implements range reduction, MUFU.RSQ, Newton-Raphson correction, and renormalization
- FP32: `MUFU.RSQ` for approximate, helper call for IEEE-compliant

## Scheduling and Latency

MUFU instructions are scheduled on the SFU (Special Function Unit), which is functional unit index 8 in the ptxas latency model. Key scheduling properties:

| Property | Value |
|---|---|
| Functional unit | SFU (index 8) |
| Issue latency | 1 cycle (can issue every cycle) |
| Result latency | ~4 cycles (pipeline depth) |
| Throughput | 1 per 4 cycles per SM partition (16 per SM for 4 partitions) |
| Dual-issue | Cannot dual-issue with ALU on same warp |

The scheduler (`sub_815820`) places MUFU instructions to maximize overlap with ALU operations from other warps. The Newton-Raphson sequences interleave MUFU, DFMA, IMAD, and MOV instructions to hide the SFU pipeline latency behind ALU computation.

## Fast-Math vs IEEE-Compliant Summary

| PTX Operation | Fast-Math (`-use_fast_math`) | IEEE-Compliant |
|---|---|---|
| `div.f32` | `MUFU.RCP` + `FMUL` (2 instr) | `__cuda_sm20_div_rn_f32` call (~15 instr) |
| `div.f64` | N/A (no FP64 fast-math) | DDIV template (~100--120 instr) |
| `rcp.f32` | `MUFU.RCP` (1 instr) | `__cuda_sm20_rcp_rn_f32` call (~10 instr) |
| `rcp.f64` | N/A | DRCP template (~90 instr) |
| `sqrt.f32` | `MUFU.RSQ` + `FMUL` (2 instr) | `__cuda_sm20_sqrt_rn_f32` call (~12 instr) |
| `sqrt.f64` | N/A | DSQRT template (~80 instr) |
| `rsqrt.f32` | `MUFU.RSQ` (1 instr) | `__cuda_sm20_drsqrt_f64_v2` (for f64) |
| `sin.f32` | `MUFU.SIN` (1 instr) | Range reduction + `MUFU.SIN` + correction |
| `cos.f32` | `MUFU.COS` (1 instr) | Range reduction + `MUFU.COS` + correction |
| `ex2.f32` | `MUFU.EX2` (1 instr) | Range reduction + `MUFU.EX2` + correction |
| `lg2.f32` | `MUFU.LG2` (1 instr) | Range reduction + `MUFU.LG2` + correction |

## Key Function Reference

| Address | Size | Identity |
|---|---|---|
| `sub_5B76D0` | 64 KB | `div` codegen handler -- dispatches all division variants |
| `sub_5B0CD0` | 44 KB | `rcp` codegen handler -- reciprocal for f32/f64 |
| `sub_5B4040` | 49 KB | `sqrt` codegen handler -- square root for f32/f64 |
| `sub_57BFC0` | ~10 KB | `rsqrt` codegen handler -- reciprocal square root |
| `sub_583190` | ~14 KB | `ex2` codegen handler -- base-2 exponential |
| `sub_52A5C0` | ~5 KB | `lg2` codegen handler -- base-2 logarithm |
| `sub_505B00` | ~5 KB | `tanh` codegen handler -- hyperbolic tangent |
| `sub_573860` | ~7 KB | `div.full` codegen handler -- FP64 full-precision division |
| `sub_589810` | ~13 KB | `rem` codegen handler -- integer remainder |
| `sub_5D1660` | 46 KB | Master intrinsic registration (608 entries) |
| `sub_5FF700` | 354 KB | Prototype generator (PTX `.weak .func` declarations) |
| `sub_80E9B0` | ~1.5 KB | `LowerSpecialFunctions` -- MUFU emission pass |
| `sub_170E8B0` | -- | DDIV top-level handler |
| `sub_1718D60` | 790 B | DRCP/DSQRT coordinator wrapper |
| `sub_17276C0` | 1,011 B | DRSQRT coordinator wrapper |
| `sub_1704070` | 263 B | DDIV register-pressure dispatcher |
| `sub_1724A20` | 28 KB | 32-bit integer division via MUFU.RCP |
| `sub_1728930` | 16.5 KB | 64-bit unsigned integer division |
| `sub_1727AC0` | 13.8 KB | 64-bit signed integer division |
| `sub_AED3C0` | 28 KB | Master lowering dispatcher (invokes all templates) |
| `sub_10C0170` | ~5 KB | MUFU Mercury encoding function (sm_100+) |
