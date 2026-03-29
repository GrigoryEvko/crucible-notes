# Surface and Texture Builtins

Surface and texture builtins form the largest contiguous block in the builtin table, with 165 surface store entries (IDs 474--638) plus a generic texture/surface handler (ID 647). CUDA separates texture reads (which go through a unified handler) from surface writes (which have dedicated per-format builtins). This asymmetry reflects the hardware: texture reads use a programmable texture pipeline, while surface stores map directly to typed `sust` (surface store) instructions.

## Surface Store Builtins (IDs 474--638)

The 165 `sust` (surface store) builtins encode the dimensionality, data type, and out-of-bounds behavior directly in the builtin name. They follow the pattern:

```
__nvvm_sust_b_{dim}_{type}_{oob_mode}
```

### Dimensions (5 variants)

| Dimension | Description |
|---|---|
| `1d` | One-dimensional surface |
| `2d` | Two-dimensional surface |
| `3d` | Three-dimensional surface |
| `1d_array` | Array of 1D surfaces |
| `2d_array` | Array of 2D surfaces |

### Data Types (11 variants)

| Type Suffix | Element Size | Vector |
|---|---|---|
| `i8` | 8-bit integer | Scalar |
| `i16` | 16-bit integer | Scalar |
| `i32` | 32-bit integer | Scalar |
| `i64` | 64-bit integer | Scalar |
| `v2i8` | 8-bit integer | 2-element vector |
| `v2i16` | 16-bit integer | 2-element vector |
| `v2i32` | 32-bit integer | 2-element vector |
| `v2i64` | 64-bit integer | 2-element vector |
| `v4i8` | 8-bit integer | 4-element vector |
| `v4i16` | 16-bit integer | 4-element vector |
| `v4i32` | 32-bit integer | 4-element vector |

### Out-of-Bounds Modes (3 variants)

| Mode | ID Range | Behavior |
|---|---|---|
| `clamp` | 474--528 | Clamp coordinates to valid range |
| `trap` | 529--583 | Trigger hardware trap on OOB access |
| `zero` | 584--638 | Write zero for OOB coordinates |

The total 5 x 11 x 3 = 165 entries are registered as a contiguous block. IDA shows SSE `xmmword` constant loads for the long common prefix strings (`__nvvm_sust_b_2d_array_*`), which is the compiler's optimization of string literal initialization during registration.

## Texture/Surface Read Handler (ID 647)

All texture reads and surface reads are funneled through a single generic handler:

| ID | Builtin | Description |
|---|---|---|
| 647 | `__nv_tex_surf_handler` | Dispatch for all texture/surface read operations |

Unlike the surface stores which have 165 dedicated builtins, texture reads use a string-based dispatch mechanism. The lowering handler (case `0x287` in `sub_955A70`) performs:

1. **String extraction** -- Walks the AST operand tree to find the constant string name of the texture/surface operation. Validates that byte 173 of the operand equals 2 (constant string marker).

2. **Element type determination** -- Decodes the element type from the AST type node. Supported types:
   - `void`, `char`, `schar`, `uchar`
   - `short`, `ushort`, `int`, `uint`
   - `long`, `ulong`, `longlong`, `ulonglong`
   - `float`

3. **Intrinsic name construction** -- Concatenates the surface/texture operation name with the element type: `"{operation}_{element_type}"`.

4. **Intrinsic lookup** -- Resolves the constructed name via `sub_1632190` (EDG) / `sub_BA8CA0` (NVVM) to obtain the corresponding LLVM intrinsic.

5. **Call emission** -- Passes all arguments through to the intrinsic call. Returns a dummy `i32` value via `sub_AD6530`.

This design allows the compiler to support an arbitrary number of texture/surface read variants without enumerating them in the builtin table.

## Texture/Surface Map Initialization

The NVVM-side handler `sub_954F10` maintains two lazily-initialized red-black tree maps for resolving texture and surface operations:

### Surface Operation Map (`unk_4F6D3C0`)

Used when the handler's `v8` flag is nonzero. Contains entries mapping builtin IDs to LLVM intrinsic IDs for surface read operations:

| Intrinsic ID | Description |
|---|---|
| `0x21CA` (8650) | Surface read (primary) |

### Texture Operation Map (`unk_4F6D380`)

Contains entries for texture fetch operations:

| Intrinsic ID | Mapped Builtin Base | Description |
|---|---|---|
| `0x1FC6` (8134) | ID 338 | Texture fetch (sync variant) |
| `0x23C5` (9157) | ID 302 | Texture fetch (base variant) |
| `0x23C8` (9160) | ID 303 | Texture fetch (alternate) |

The map contains 12 entries total covering different texture fetch modes (filtered, unfiltered, LOD, gradient).

### Operand Processing

For each of the 4 standard texture operands (sampler, coordinate, LOD, bias), the handler:
- Checks if the operand is non-null
- Type-casts to match the expected LLVM type
- Creates a store instruction via `sub_B4D190` (loads) or `sub_B4D3C0` (stores)
- Builds the LLVM call via `sub_90A810` with the resolved intrinsic ID

## Surface Store Lowering Details

Surface store builtins in the 474--638 range are handled by the main dispatch switch with a block of consecutive cases. Each case:

1. Extracts the surface handle, coordinate(s), and data value(s) from the argument list
2. The number of coordinate arguments varies by dimensionality (1D: 1, 2D: 2, 3D: 3, arrays: +1 for layer index)
3. The number of data arguments varies by vector width (scalar: 1, v2: 2, v4: 4)
4. Emits a call to the corresponding `llvm.nvvm.sust.b.*` intrinsic

The out-of-bounds mode is encoded in the intrinsic name itself, not as a parameter, which is why each mode requires a separate builtin ID.

## Architecture Considerations

Surface and texture operations are available on all SM architectures. However, the texture pipeline has evolved significantly:

- **All SM**: Basic texture fetch, surface read/write with clamp/trap/zero modes
- **SM 30+**: Surface load/store with `__nv_tex_surf_handler` generic dispatch
- **SM 90+ (Hopper)**: Tensor memory accelerator (TMA) operations provide an alternative high-throughput path for bulk data movement, partially overlapping with texture/surface functionality but handled through separate builtins (IDs 411--412)

The 165 surface store builtins are registered unconditionally regardless of target SM. Architecture gating occurs at the PTX emission layer, not during builtin registration or lowering.
