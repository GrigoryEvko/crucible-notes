# FP128/I128 Emulation

No NVIDIA GPU in any SM generation has native 128-bit arithmetic hardware. Neither `fp128` (IEEE 754 binary128) nor `i128` (128-bit integer) operations can be lowered to PTX instructions directly. CICC handles this by replacing every `fp128` and `i128` operation in LLVM IR with a call to one of 48 distinct NVIDIA runtime library functions whose implementations live in a separate bitcode module. The pass at `sub_1C8C170` walks each function in the module, inspects every instruction, dispatches on the LLVM opcode byte, and emits the appropriate `__nv_*` call in place of the original operation. This is a correctness-critical legalization pass -- if any `fp128`/`i128` operation survives past it, instruction selection will abort because NVPTX has no patterns for 128-bit types.

The pass is structurally part of `lower-ops` (`LowerOpsPass`), NVIDIA's umbrella module pass for lowering operations that the NVPTX backend cannot handle natively. Within the `lower-ops` framework, `sub_1C8C170` is the dedicated handler for 128-bit types. It runs as a module-level pass early in the pipeline, after libdevice linking and before the main optimization sequence, so that the generated calls can be inlined and optimized by subsequent passes.

| | |
|---|---|
| **Entry point** | `sub_1C8C170` |
| **Size** | 25 KB (~960 lines decompiled) |
| **Pass framework** | Part of `lower-ops` / `LowerOpsPass` (module pass) |
| **Registration** | New PM slot 144 at `sub_2342890`; param `enable-optimization` |
| **Runtime functions** | 48 distinct `__nv_*` library calls |
| **Upstream equivalent** | None. Upstream LLVM lowers fp128 through SoftenFloat in type legalization. CICC replaces this with explicit call insertion at the IR level. |

## Opcode Dispatch

The pass reads the LLVM instruction opcode from the byte at offset `+16` of the instruction node and dispatches through a dense switch. The following table lists every handled opcode and the corresponding lowering action. All unlisted opcodes in the range `0x18`--`0x58` produce an early return (no 128-bit type involvement, or handled elsewhere).

| Opcode | LLVM Instruction | Lowering Target | Handler |
|---|---|---|---|
| `0x24` | `fadd` | `__nv_add_fp128` | `sub_1C8A5C0` |
| `0x26` | `fsub` | `__nv_sub_fp128` | `sub_1C8A5C0` |
| `0x28` | `fmul` | `__nv_mul_fp128` | `sub_1C8A5C0` |
| `0x29` | `udiv` | `__nv_udiv128` | `sub_1C8BD70` |
| `0x2A` | `sdiv` | `__nv_idiv128` | `sub_1C8BD70` |
| `0x2B` | `fdiv` | `__nv_div_fp128` | `sub_1C8A5C0` |
| `0x2C` | `urem` | `__nv_urem128` | `sub_1C8BD70` |
| `0x2D` | `srem` | `__nv_irem128` | `sub_1C8BD70` |
| `0x2E` | `frem` | `__nv_rem_fp128` | `sub_1C8A5C0` |
| `0x36` | `trunc`/`ext` | Type-based conversion | `sub_1C8ADC0` |
| `0x3F` | `fptoui` | `__nv_fp128_to_uint*` or `__nv_cvt_f*_u128_rz` | `sub_1C8ADC0` / `sub_1C8BF90` |
| `0x40` | `fptosi` | `__nv_fp128_to_int*` or `__nv_cvt_f*_i128_rz` | `sub_1C8ADC0` / `sub_1C8BF90` |
| `0x41` | `uitofp` | `__nv_uint*_to_fp128` or `__nv_cvt_u128_f*_rn` | `sub_1C8ADC0` / `sub_1C8BF90` |
| `0x42` | `sitofp` | `__nv_int*_to_fp128` or `__nv_cvt_i128_f*_rn` | `sub_1C8ADC0` / `sub_1C8BF90` |
| `0x43` | `fptrunc` | `__nv_fp128_to_float` or `__nv_fp128_to_double` | `sub_1C8ADC0` |
| `0x44` | `fpext` | `__nv_float_to_fp128` or `__nv_double_to_fp128` | `sub_1C8ADC0` |
| `0x4C` | `fcmp` | `__nv_fcmp_*` (predicate-selected) | dedicated |

Ignored opcode ranges: `0x18`--`0x23`, `0x25`, `0x27`, `0x2F`--`0x35`, `0x37`--`0x3E`, `0x45`--`0x4B`, `0x4D`--`0x58`. Opcode `0x37` (`store`) receives a similar type check as `0x36` but for store target types.

## Library Function Inventory

### FP128 Arithmetic (5 functions)

Binary operations on IEEE 754 binary128. Each takes two `fp128` operands, returns `fp128`.

| Function | Operation | String Length |
|---|---|---|
| `__nv_add_fp128` | `fp128` addition | 14 |
| `__nv_sub_fp128` | `fp128` subtraction | 14 |
| `__nv_mul_fp128` | `fp128` multiplication | 14 |
| `__nv_div_fp128` | `fp128` division | 14 |
| `__nv_rem_fp128` | `fp128` remainder | 14 |

All five are lowered through `sub_1C8A5C0`, which constructs the call with a fixed string length of 14 characters.

### I128 Division and Remainder (4 functions)

Integer division and remainder for `i128`. No native PTX instruction exists for 128-bit integer divide.

| Function | Operation | Signedness | String Length |
|---|---|---|---|
| `__nv_udiv128` | `i128` division | unsigned | 12 |
| `__nv_idiv128` | `i128` division | signed | 12 |
| `__nv_urem128` | `i128` remainder | unsigned | 12 |
| `__nv_irem128` | `i128` remainder | signed | 12 |

Lowered through `sub_1C8BD70` with string length 12. Note: `i128` add/sub/mul are NOT lowered here -- those can be decomposed into pairs of 64-bit operations by standard LLVM legalization. Only division and remainder require the runtime call path because they involve complex multi-word algorithms.

### FP128-to-Integer Conversions (10 functions)

Convert `fp128` to integer types of various widths. The target width is determined by examining `sub_1642F90` and the type's bit-width field (`type_id >> 8`).

| Function | Conversion |
|---|---|
| `__nv_fp128_to_uint8` | `fp128` -> `i8` (unsigned) |
| `__nv_fp128_to_uint16` | `fp128` -> `i16` (unsigned) |
| `__nv_fp128_to_uint32` | `fp128` -> `i32` (unsigned) |
| `__nv_fp128_to_uint64` | `fp128` -> `i64` (unsigned) |
| `__nv_fp128_to_uint128` | `fp128` -> `i128` (unsigned) |
| `__nv_fp128_to_int8` | `fp128` -> `i8` (signed) |
| `__nv_fp128_to_int16` | `fp128` -> `i16` (signed) |
| `__nv_fp128_to_int32` | `fp128` -> `i32` (signed) |
| `__nv_fp128_to_int64` | `fp128` -> `i64` (signed) |
| `__nv_fp128_to_int128` | `fp128` -> `i128` (signed) |

### Integer-to-FP128 Conversions (10 functions)

Convert integer types to `fp128`.

| Function | Conversion |
|---|---|
| `__nv_uint8_to_fp128` | `i8` (unsigned) -> `fp128` |
| `__nv_uint16_to_fp128` | `i16` (unsigned) -> `fp128` |
| `__nv_uint32_to_fp128` | `i32` (unsigned) -> `fp128` |
| `__nv_uint64_to_fp128` | `i64` (unsigned) -> `fp128` |
| `__nv_uint128_to_fp128` | `i128` (unsigned) -> `fp128` |
| `__nv_int8_to_fp128` | `i8` (signed) -> `fp128` |
| `__nv_int16_to_fp128` | `i16` (signed) -> `fp128` |
| `__nv_int32_to_fp128` | `i32` (signed) -> `fp128` |
| `__nv_int64_to_fp128` | `i64` (signed) -> `fp128` |
| `__nv_int128_to_fp128` | `i128` (signed) -> `fp128` |

String lengths for both fp128-to-integer and integer-to-fp128 conversions vary from 18 to 21 characters depending on the function name. Lowered through `sub_1C8ADC0`.

### FP128-to-Float/Double Conversions (4 functions)

Truncation and extension between `fp128` and the native floating-point types.

| Function | Conversion | Opcode |
|---|---|---|
| `__nv_fp128_to_float` | `fp128` -> `float` | `0x43` (`fptrunc`) |
| `__nv_fp128_to_double` | `fp128` -> `double` | `0x43` (`fptrunc`) |
| `__nv_float_to_fp128` | `float` -> `fp128` | `0x44` (`fpext`) |
| `__nv_double_to_fp128` | `double` -> `fp128` | `0x44` (`fpext`) |

### I128-to-Float/Double Conversions (8 functions)

These handle the non-fp128 path: converting `i128` directly to/from `float`/`double` without going through `fp128` as an intermediate. The `_rz` suffix denotes round-toward-zero mode; `_rn` denotes round-to-nearest-even.

| Function | Conversion | Rounding | String Length |
|---|---|---|---|
| `__nv_cvt_f32_u128_rz` | `i128` (unsigned) -> `float` | toward zero | 20 |
| `__nv_cvt_f32_i128_rz` | `i128` (signed) -> `float` | toward zero | 20 |
| `__nv_cvt_f64_u128_rz` | `i128` (unsigned) -> `double` | toward zero | 20 |
| `__nv_cvt_f64_i128_rz` | `i128` (signed) -> `double` | toward zero | 20 |
| `__nv_cvt_u128_f32_rn` | `float` -> `i128` (unsigned) | to nearest | 20 |
| `__nv_cvt_i128_f32_rn` | `float` -> `i128` (signed) | to nearest | 20 |
| `__nv_cvt_u128_f64_rn` | `double` -> `i128` (unsigned) | to nearest | 20 |
| `__nv_cvt_i128_f64_rn` | `double` -> `i128` (signed) | to nearest | 20 |

All eight are lowered through `sub_1C8BF90` with a fixed string length of 20 characters. The rounding mode choice is deliberate: `_rz` for integer-from-float (truncation semantics matching C/C++ cast behavior) and `_rn` for float-from-integer (IEEE 754 default rounding for conversions).

The dispatch logic selects between the `__nv_fp128_to_*` / `__nv_*_to_fp128` family and the `__nv_cvt_*` family based on whether the source or destination type is `fp128` (type_id == 5). If neither operand is `fp128` but one is `i128`, the `__nv_cvt_*` path is taken.

## FP128 Comparison Predicates

The `fcmp` instruction (opcode `0x4C`) is dispatched by extracting the comparison predicate from bits 0--14 of the halfword at instruction offset `+18`. Each LLVM `fcmp` predicate maps to a dedicated runtime function.

### Ordered Comparisons (7 functions)

Ordered comparisons return false if either operand is NaN.

| Function | Predicate | Semantics |
|---|---|---|
| `__nv_fcmp_oeq` | `oeq` | ordered equal |
| `__nv_fcmp_ogt` | `ogt` | ordered greater-than |
| `__nv_fcmp_oge` | `oge` | ordered greater-or-equal |
| `__nv_fcmp_olt` | `olt` | ordered less-than |
| `__nv_fcmp_ole` | `ole` | ordered less-or-equal |
| `__nv_fcmp_one` | `one` | ordered not-equal |
| `__nv_fcmp_ord` | `ord` | ordered (neither is NaN) |

### Unordered Comparisons (7 functions)

Unordered comparisons return true if either operand is NaN.

| Function | Predicate | Semantics |
|---|---|---|
| `__nv_fcmp_uno` | `uno` | unordered (either is NaN) |
| `__nv_fcmp_ueq` | `ueq` | unordered or equal |
| `__nv_fcmp_ugt` | `ugt` | unordered or greater-than |
| `__nv_fcmp_uge` | `uge` | unordered or greater-or-equal |
| `__nv_fcmp_ult` | `ult` | unordered or less-than |
| `__nv_fcmp_ule` | `ule` | unordered or less-or-equal |
| `__nv_fcmp_une` | `une` | unordered or not-equal |

The predicate naming follows the standard LLVM `fcmp` convention: `o` prefix = ordered, `u` prefix = unordered. The 14 predicates cover the complete set of IEEE 754 comparison semantics excluding `true` and `false` (which are constant-folded before reaching this pass). Each function takes two `fp128` operands and returns `i1`.

## Trunc/Ext Handling (Opcode 0x36)

The `trunc`/`zext`/`sext` opcode path requires special logic because it must distinguish between genuine 128-bit truncation/extension and other type conversions that happen to use the same opcode.

```c
sub_1C8C170::handle_trunc_ext(inst):
    if sub_1642F90(*operand, 128):      // Is the operand type 128-bit?
        // Determine source and dest bit-widths from DataLayout
        src_bits = type_id >> 8          // Bit-width encoded in high byte
        dst_bits = target_type_id >> 8
        if src_bits > dst_bits:
            emit_truncation(inst, src_bits, dst_bits)
        else:
            emit_extension(inst, src_bits, dst_bits, is_signed)
    elif type_id == 5:                   // fp128 type marker
        emit_fp128_conversion(inst)
    else:
        return                           // Not a 128-bit operation
```

The type_id value `5` is the LLVM type tag for `fp128` in CICC's internal representation (consistent with the type code table: `1`=half, `2`=float, `3`=double, `4`=fp80, `5`=fp128, `6`=bf16, `0xB`=integer with bit-width at `type_id >> 8`).

## Lowering Helpers

Four internal helper functions perform the actual call construction. Each creates a new `CallInst` with the library function name, replaces all uses of the original instruction with the call result, and erases the original instruction.

| Helper | Address | Purpose | Name Length |
|---|---|---|---|
| `sub_1C8A5C0` | `0x1C8A5C0` | Binary `fp128` arithmetic (add/sub/mul/div/rem) | 14 |
| `sub_1C8BD70` | `0x1C8BD70` | Binary `i128` division (udiv/idiv/urem/irem) | 12 |
| `sub_1C8ADC0` | `0x1C8ADC0` | FP128 conversions (to/from all integer widths, to/from float/double) | 18--21 (varies) |
| `sub_1C8BF90` | `0x1C8BF90` | I128-to/from-float/double conversions | 20 |

The "name length" column refers to the string length passed to the call construction routine. This is a fixed constant in each helper, not computed at runtime, which means the function name strings are embedded as literals in the binary (confirmed by string sweep at `0x1C8C170`).

Each helper follows the same pattern:

```c
helper(module, instruction, name_string, name_length):
    // 1. Get or create function declaration in module
    func = module.getOrInsertFunction(name_string, return_type, param_types...)
    // 2. Build argument list from instruction operands
    args = extract_operands(instruction)
    // 3. Create CallInst
    call = IRBuilder.CreateCall(func, args)
    // 4. Replace uses and erase
    instruction.replaceAllUsesWith(call)
    instruction.eraseFromParent()
```

## Libdevice Resolution

The 48 `__nv_*` functions emitted by this pass are **not** present in the standard `libdevice.10.bc`. The standard libdevice (455,876 bytes embedded at `unk_3EA0080` / `unk_420FD80`) contains ~400+ math functions (`__nv_sinf`, `__nv_expf`, etc.) but does not include any `fp128` or `i128` emulation routines.

Instead, these functions are resolved through one of two mechanisms:

1. **Separate bitcode library**: A dedicated 128-bit emulation bitcode module linked after `lower-ops` runs. This module contains the actual multi-word software implementations of 128-bit arithmetic using 64-bit operations.

2. **Late synthesis during type legalization**: The SelectionDAG type legalization pass (`SoftenFloat` action) can also handle `fp128` operations, but CICC's IR-level lowering preempts this by replacing operations before they reach the backend. The `__nv_*` functions, once declared in the module, must be resolvable at link time.

The call declarations emitted by the pass use external linkage, meaning the linker must supply definitions. If a definition is missing, the compilation will fail at the NVPTX link stage with an unresolved symbol error. The benefit of performing this lowering at the IR level rather than in SelectionDAG is that the resulting calls are visible to the LLVM optimizer: the inliner can inline the emulation routines, SROA can decompose the intermediate values, and the loop optimizers can hoist invariant 128-bit computations.

## Configuration

The pass has no dedicated knobs. It is controlled indirectly through the `lower-ops` pass framework:

| Parameter | Effect |
|---|---|
| `enable-optimization` | Parameter to `LowerOpsPass` registration (slot 144). When enabled, the lowered calls may be marked with optimization attributes. |

There are no knobs in `knobs.txt` specific to `fp128` or `i128` lowering. The pass runs unconditionally whenever `lower-ops` is in the pipeline -- there is no way to disable 128-bit emulation because leaving `fp128`/`i128` operations in the IR would cause a fatal error in the NVPTX backend.

## Diagnostic Strings

The pass itself emits no diagnostic messages or debug prints. All diagnostic information comes from the embedded function name strings:

```text
"__nv_add_fp128"         "__nv_sub_fp128"         "__nv_mul_fp128"
"__nv_div_fp128"         "__nv_rem_fp128"
"__nv_udiv128"           "__nv_idiv128"
"__nv_urem128"           "__nv_irem128"
"__nv_fp128_to_uint8"    "__nv_fp128_to_int8"
"__nv_fp128_to_uint16"   "__nv_fp128_to_int16"
"__nv_fp128_to_uint32"   "__nv_fp128_to_int32"
"__nv_fp128_to_uint64"   "__nv_fp128_to_int64"
"__nv_fp128_to_uint128"  "__nv_fp128_to_int128"
"__nv_uint8_to_fp128"    "__nv_int8_to_fp128"
"__nv_uint16_to_fp128"   "__nv_int16_to_fp128"
"__nv_uint32_to_fp128"   "__nv_int32_to_fp128"
"__nv_uint64_to_fp128"   "__nv_int64_to_fp128"
"__nv_uint128_to_fp128"  "__nv_int128_to_fp128"
"__nv_fp128_to_float"    "__nv_fp128_to_double"
"__nv_float_to_fp128"    "__nv_double_to_fp128"
"__nv_cvt_f32_u128_rz"   "__nv_cvt_f32_i128_rz"
"__nv_cvt_f64_u128_rz"   "__nv_cvt_f64_i128_rz"
"__nv_cvt_u128_f32_rn"   "__nv_cvt_i128_f32_rn"
"__nv_cvt_u128_f64_rn"   "__nv_cvt_i128_f64_rn"
"__nv_fcmp_oeq"          "__nv_fcmp_ogt"          "__nv_fcmp_oge"
"__nv_fcmp_olt"          "__nv_fcmp_ole"          "__nv_fcmp_one"
"__nv_fcmp_ord"          "__nv_fcmp_uno"          "__nv_fcmp_ueq"
"__nv_fcmp_ugt"          "__nv_fcmp_uge"          "__nv_fcmp_ult"
"__nv_fcmp_ule"          "__nv_fcmp_une"
```

## Function Map

| Function | Address | Size | Role |
|---|---|---|---|
| Main entry | `sub_1C8C170` | 25 KB | Opcode dispatch, instruction walk, type checks |
| FP128 binary lowering | `sub_1C8A5C0` | -- | Emits `__nv_{add,sub,mul,div,rem}_fp128` calls |
| FP128 conversion lowering | `sub_1C8ADC0` | -- | Emits `__nv_fp128_to_*` / `__nv_*_to_fp128` calls |
| I128 division lowering | `sub_1C8BD70` | -- | Emits `__nv_{u,i}div128` / `__nv_{u,i}rem128` calls |
| I128-float lowering | `sub_1C8BF90` | -- | Emits `__nv_cvt_*` calls (rz/rn variants) |
| Type width check | `sub_1642F90` | -- | Tests whether a type has a given bit-width (e.g., 128) |

## Cross-References

- [NVIDIA Custom Passes](./index.md) -- pass registry including `lower-ops`
- [Other NVIDIA Passes](./other.md) -- summary entry for this pass
- [Type Legalization](../llvm/type-legalization.md) -- SelectionDAG SoftenFloat path for fp128 (preempted by this pass)
- [Libdevice Linking](../infra/libdevice-linking.md) -- how the embedded libdevice is linked (standard math, not fp128)
- [Cast Codegen](../pipeline/irgen-expressions.md) -- EDG frontend cast generation, type tag `5` = fp128
- [Struct Splitting](./struct-splitting.md) -- sibling pass within the same address cluster
