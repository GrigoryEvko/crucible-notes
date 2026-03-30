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

### Surface Store ID Layout

Within each OOB-mode block of 55 entries, the ordering is dimension-major, type-minor:

```
base + 0..10:  1d       x {i8,i16,i32,i64,v2i8,v2i16,v2i32,v2i64,v4i8,v4i16,v4i32}
base + 11..21: 1d_array x {i8,i16,i32,i64,v2i8,v2i16,v2i32,v2i64,v4i8,v4i16,v4i32}
base + 22..32: 2d       x {i8,i16,i32,i64,v2i8,v2i16,v2i32,v2i64,v4i8,v4i16,v4i32}
base + 33..43: 2d_array x {i8,i16,i32,i64,v2i8,v2i16,v2i32,v2i64,v4i8,v4i16,v4i32}
base + 44..54: 3d       x {i8,i16,i32,i64,v2i8,v2i16,v2i32,v2i64,v4i8,v4i16,v4i32}
```

Given a surface store builtin ID, the decomposition is:

```
mode_offset = (id - 474)
oob_block   = mode_offset / 55          // 0=clamp, 1=trap, 2=zero
within_block = mode_offset % 55
dim_index    = within_block / 11         // 0=1d, 1=1d_array, 2=2d, 3=2d_array, 4=3d
type_index   = within_block % 11         // 0=i8 .. 10=v4i32
```

## Texture/Surface Read Handler (ID 647)

All texture reads and surface reads are funneled through a single generic handler:

| ID | Builtin | Description |
|---|---|---|
| 647 | `__nv_tex_surf_handler` | Dispatch for all texture/surface read operations |

Unlike the surface stores which have 165 dedicated builtins, texture reads use a string-based dispatch mechanism. The handler is a single builtin that receives the texture/surface operation name as a string operand, then dynamically constructs the appropriate LLVM intrinsic name and emits the call.

### Handler Dispatch Algorithm (case `0x287` in `sub_955A70`)

The NVVM-side lowering for `__nv_tex_surf_handler` (builtin ID 647, hex `0x287`) is the most complex string-based builtin dispatch in cicc. It performs five steps:

**Step 1 -- String extraction.** Walks the AST operand tree from the call expression to locate the constant string naming the texture/surface operation. Validates that byte 173 of the operand node equals `2` (the constant-string-type marker in the EDG AST). The string is the NVVM intrinsic base name, for example `__tex_fetch` or `__surf_read`.

**Step 2 -- Element type determination.** Decodes the return element type from the AST type node attached to the call. The type switch maps to suffix strings:

| AST Type | Suffix String | LLVM Type |
|---|---|---|
| `void` | `"void"` | `void` |
| `char` (as signed) | `"char_as_schar"` | `i8` |
| `char` (as unsigned) | `"char_as_uchar"` | `i8` |
| `signed char` | `"schar"` | `i8` |
| `unsigned char` | `"uchar"` | `i8` |
| `short` | `"short"` | `i16` |
| `unsigned short` | `"ushort"` | `i16` |
| `int` | `"int"` | `i32` |
| `unsigned int` | `"uint"` | `i32` |
| `long` | `"long"` | `i32`/`i64` |
| `unsigned long` | `"ulong"` | `i32`/`i64` |
| `long long` | `"longlong"` | `i64` |
| `unsigned long long` | `"ulonglong"` | `i64` |
| `float` | `"float"` | `float` |

The `long`/`ulong` width follows the host ABI convention (32-bit on NVPTX).

**Step 3 -- Intrinsic name construction.** Concatenates the operation base name with the element type suffix using underscore separation:

```
intrinsic_name = "{operation_string}_{element_type_suffix}"
```

For example, `__tex_fetch_v4` + `float` yields `__tex_fetch_v4_float`.

**Step 4 -- Intrinsic lookup.** Resolves the constructed name string via `sub_BA8CA0` (NVVM intrinsic table lookup) to obtain the corresponding LLVM intrinsic function declaration. The EDG-side parallel path uses `sub_1632190`. If the intrinsic is not found, this is a fatal error.

**Step 5 -- Call emission.** Collects all arguments from the call expression, builds the LLVM function type signature from the argument types, and emits the intrinsic call via `sub_921880`. Returns a dummy `i32` value via `sub_AD6530`.

This design allows the compiler to support an arbitrary number of texture/surface read variants without enumerating them in the builtin table. The single ID 647 entry is a trampoline that dispatches to hundreds of different NVVM intrinsics at runtime.

## `__nv_tex_surf_handle_t` Built-in Type

The EDG parser recognizes `__nv_tex_surf_handle_t` as a built-in type (keyword index 277 in the keyword table at `sub_72BA30`). This opaque type is the C++-level representation of a texture or surface reference handle. When the type appears as a function parameter, the PTX emitter (`sub_21502D0`, 22KB) produces one of:

| Parameter ABI | PTX Syntax |
|---|---|
| By-value `.texref` | `.param .texref NAME` |
| By-value `.surfref` | `.param .surfref NAME` |
| By-value `.samplerref` | `.param .samplerref NAME` |
| Pointer to `.texref` | `.param .u64 .ptr .texref NAME` |
| Pointer to `.surfref` | `.param .u64 .ptr .surfref NAME` |
| Pointer to `.samplerref` | `.param .u64 .ptr .samplerref NAME` |

The selection between `.texref` / `.surfref` / `.samplerref` is determined by the NVVM metadata attached to the GlobalVariable that the handle references. The `NVPTXReplaceImageHandles` pass (`sub_21DBEA0`) performs the final substitution of IR-level image handles into PTX-level texture/surface references during machine-level code emission.

## Texture/Surface Map Initialization

The NVVM-side handler `sub_954F10` maintains two lazily-initialized red-black tree maps for resolving texture and surface operations. These maps are built once (guarded by flag bytes `byte_4F6D3B0` and `byte_4F6D378`) and cleaned up via `__cxa_atexit`.

### Surface Operation Map (`unk_4F6D3C0`)

Used when the handler's `v8` flag is nonzero (surface path). Contains entries mapping builtin IDs to LLVM intrinsic IDs for surface read operations. Each entry is a 12-byte packed triple:

| Intrinsic ID | Description |
|---|---|
| `0x21CA` (8650) | Surface read (primary) |

The map contains 4 entries covering surface read and write variants with address space 4 (constant memory surface descriptors).

### Texture Operation Map (`unk_4F6D380`)

Contains entries for texture fetch operations. The map has 12 entries covering the full matrix of texture modes:

| Intrinsic ID | Mapped Builtin Base | Description |
|---|---|---|
| `0x1FC6` (8134) | ID 338 | Texture fetch (sync variant) |
| `0x23C5` (9157) | ID 302 | Texture fetch (base variant) |
| `0x23C8` (9160) | ID 303 | Texture fetch (alternate) |

These 12 entries span the following texture fetch modes:

| Mode | Behavior |
|---|---|
| Unfiltered fetch | Direct texel access at integer coordinates |
| Filtered fetch | Hardware-interpolated fetch at float coordinates |
| LOD fetch | Explicit level-of-detail selection |
| Gradient fetch | Gradient-based LOD computation |

### Map Lookup and Dispatch (sub_954F10)

```
function TexSurfSampleHandler(retval, ctx, builtin_id, arglist):
    // Determine surface vs texture path
    is_surface = (v8 flag != 0)

    if is_surface:
        map = unk_4F6D3C0     // surface map
        if not initialized:
            populate 4 entries into red-black tree
            byte_4F6D3B0 = 1
    else:
        map = unk_4F6D380     // texture map
        if not initialized:
            populate 12 entries into red-black tree
            byte_4F6D378 = 1

    // Tree lookup
    entry = rbtree_find(map, builtin_id)
    if found:
        intrinsic_id = entry.intrinsic_id   // e.g. 0x1FC6
    else:
        intrinsic_id = 0
        default_mode = 1

    // Create type constant from element type
    type_const = sub_BCB2D0(sub_ACD640(...))

    // Process 4 standard operands
    for operand in [sampler, coordinate, lod, bias]:
        if operand != null:
            lowered = type_cast(operand, expected_llvm_type)
            emit_store(lowered)   // sub_B4D190 or sub_B4D3C0

    // Build and emit intrinsic call
    fn_decl = sub_90A810(intrinsic_tables, intrinsic_id, ...)
    sub_921880(fn_decl, args)        // emit call
    sub_B4D3C0(result)               // store result
```

### Operand Processing

For each of the 4 standard texture operands (sampler, coordinate, LOD, bias), the handler:
1. Checks if the operand is non-null
2. Type-casts to match the expected LLVM type
3. Creates a store instruction via `sub_B4D190` (loads) or `sub_B4D3C0` (stores)
4. Builds the LLVM call via `sub_90A810` with the resolved intrinsic ID

## SelectionDAG Lowering Layer

After NVVM builtin lowering produces LLVM IR intrinsic calls, the SelectionDAG layer translates these into NVPTX-specific DAG nodes. Three subsystems handle different aspects.

### Intrinsic Lowering Dispatch (`sub_33B0210`, 343KB)

The central intrinsic lowering function dispatches on LLVM intrinsic IDs via a giant switch covering ~440 case labels. Texture and surface operations occupy three distinct ID ranges:

| Intrinsic ID Range | Handler | Category |
|---|---|---|
| `0x5D`--`0x8D` (93--141) | `sub_33A4350` | Texture fetch bulk handler (50 IDs) |
| `0x8E`--`0x90` (142--144) | `sub_33A3180` | Surface read/write handler (3 IDs) |
| `0x91` (145) | Inline | Complex texture sample with LOD/bias |
| `0x92`--`0x98` (146--152) | Various | Surface store variants |
| `0x9C`--`0x9D` (156--157) | `sub_33AEC60` | Surface atomics |
| `0x9E`--`0x9F` (158--159) | `sub_33AFBA0` / `sub_340EC60` | Surface special ops |
| `0xA0`--`0xA2` (160--162) | Various | Surface/texture helpers |
| `0x2952` (10578) | Inline | `nvvm_texsurf_handle` binding |
| `0x254D`+ (9549+) | `sub_34B8FD0` | Unified texture sample core |

### Texture Fetch Bulk Handler: `sub_33A4350`

The 50 consecutive intrinsic IDs `0x5D` through `0x8D` all delegate to a single helper `sub_33A4350(state, dag_node)`. This function maps the intrinsic ID to an NVPTXISD opcode for one of the `tex.1d`, `tex.2d`, `tex.3d`, or `tex.a1d`/`tex.a2d` (array) variants.

The intrinsic-to-opcode mapping encodes:

```
dimension:    1d / 2d / 3d / 1d_array / 2d_array / cubemap
data_type:    u32 / s32 / f32 / f32f32 (filtered)
return_width: scalar / v2 / v4
access_mode:  level / grad / unified
```

Each opcode corresponds to a PTX texture instruction pattern that the instruction emitter will later produce.

### Complex Texture Sample (Intrinsic ID `0x91`)

The most complex texture lowering path. Handles hardware-filtered texture sampling with programmable LOD computation:

1. `sub_3281100` -- Determines element count for the return type
2. `sub_3281590` -- Computes alignment for the result buffer
3. `sub_327FD70` -- Resolves the return MVT (machine value type)
4. `sub_33CC4A0` -- SM-specific path selection (some SM levels use different instruction encodings)
5. `sub_3406EB0(opcode=57)` -- Creates the core sample DAG node
6. `sub_33FAF80(opcode=213)` -- LOD computation DAG node
7. `sub_3406EB0(opcode=186)` -- Merge result node
8. `sub_33FAF80(opcode=389)` -- Final type fixup
9. Fallback via `sub_33A1E80` if the target architecture does not support this texture mode

### Surface Read/Write Handler: `sub_33A3180`

Intrinsic IDs `0x8E` (surf1Dread), `0x8F` (surf2Dread), `0x90` (surf3Dread) delegate to `sub_33A3180(state, dag_node, intrinsic_id)`. The `intrinsic_id` parameter selects the dimensionality. This handler produces NVPTXISD `suld` (surface load) DAG nodes.

### Texture/Surface Handle Binding (Intrinsic `0x2952`)

The `nvvm_texsurf_handle` intrinsic (ID 10578) is the mechanism for binding a GlobalVariable to a texture or surface reference. The DAG lowering:

1. Validates that operand 0 is metadata wrapping a `GlobalVariable` -- errors with `"nvvm_texsurf_handle op0 must be metadata wrapping a GlobalVariable"` otherwise
2. Creates a DAG constant node for the handle via `sub_3400BD0(opcode=10579)`
3. Binds the handle via `sub_3406EB0(opcode=46)`

The `NVPTXReplaceImageHandles` pass (`sub_21DBEA0`) later resolves these abstract handles into concrete PTX `.texref` / `.surfref` globals during machine-level emission.

### Unified Texture Sample Core (Intrinsic IDs `0x254D`+)

For SM 30+ unified texture mode, a more complex sampling path handles the full matrix of texture configurations:

1. `sub_34B8FD0` -- Unpacks the parameter block encoding dimension, filtering, coordinate type
2. Vtable dispatch at `*src+88` -- Selects the sampling mode (point, linear, etc.)
3. `sub_3409320` -- Creates the sampler state DAG node
4. `sub_33EB1C0(opcode=47)` -- Creates the core tex/surf sample DAG node with memory semantics
5. `sub_33FC220(opcode=2)` -- Merges vector result components
6. `sub_33E5830` + `sub_3411630(opcode=55)` -- Packages the final result
7. `sub_B91FC0` -- Attaches debug info

Two modes exist: `v2637=true` (unified texture) and `v2637=false` (legacy separate-handle texture). The unified path is the modern default.

### Texture/Surface Binding Lowering (Intrinsic IDs `0x44`, `0x45`, `0x47`)

These intrinsics handle the compile-time binding of texture and surface references. The lowering checks the `a1+120` flag to determine whether the reference is a `.texref` or `.surfref`:

1. `sub_3382030` -- Initial binding setup
2. `sub_3382930` -- Variant analysis via `sub_3380DB0` and `sub_B58DC0`
3. `sub_3386E40` -- Final binding emission

Intrinsic `0x48` (opcode 332) handles global texture handles, while `0x162` (opcode 331) handles sampler handles. Intrinsic `0x169` dispatches to `sub_3400BD0` + `sub_3406EB0(opcode=333)` for indirect texture access.

## Instruction Selection: `sub_306A930` (52KB)

The NVPTX instruction selection pass contains a 52KB handler (`sub_306A930`) dedicated to matching texture/surface DAG nodes to machine instructions. It calls five helper functions:

| Helper | Address | Role |
|---|---|---|
| `sub_2FE5F00` | `0x2FE5F00` | Texture instruction type selection |
| `sub_2FE5F30` | `0x2FE5F30` | Surface instruction type selection |
| `sub_2FE5F60` | `0x2FE5F60` | Image type validation |
| `sub_2FE69A0` | `0x2FE69A0` | Coordinate mode encoding |
| `sub_2FE6CC0` | `0x2FE6CC0` | Return type dispatch |

The ISel handler selects among `tex`, `suld`, `sust` machine instruction patterns, with address space awareness for the different texture/surface memory regions.

## Image Type Validation: `sub_21DD1A0` (16KB)

A dedicated 16KB validation function (`sub_21DD1A0`) checks that the image type encoding is legal for the instruction class. Four error messages cover the instruction categories:

| Error String | Instruction Class |
|---|---|
| `"Invalid image type in .tex"` | Texture fetch |
| `"Invalid image type in .suld"` | Surface load |
| `"Invalid image type in suq."` | Surface query |
| `"Invalid image type in .sust"` | Surface store |

This validation occurs during instruction emission, catching type mismatches that survived earlier lowering.

## Surface Store Lowering Details

Surface store builtins in the 474--638 range are handled by the main dispatch switch with a block of consecutive cases. Each case:

1. Extracts the surface handle, coordinate(s), and data value(s) from the argument list
2. The number of coordinate arguments varies by dimensionality (1D: 1, 2D: 2, 3D: 3, arrays: +1 for layer index)
3. The number of data arguments varies by vector width (scalar: 1, v2: 2, v4: 4)
4. Emits a call to the corresponding `llvm.nvvm.sust.b.*` intrinsic

The out-of-bounds mode is encoded in the intrinsic name itself, not as a parameter, which is why each mode requires a separate builtin ID.

## PTX Emission: Sampler State Initializers

The PTX emitter `sub_2156420` (20KB) handles module-level emission of texture, surface, and sampler global variables. Sampler references receive structured initializers:

```ptx
.global .samplerref my_sampler = {
    addr_mode_0 = wrap,          // or clamp_to_border, clamp_to_edge, mirror
    addr_mode_1 = clamp_to_edge,
    addr_mode_2 = clamp_to_edge,
    filter_mode = linear,        // or nearest
    force_unnormalized_coords = 1
};
```

The addressing mode and filter mode values are extracted from NVVM metadata attached to the sampler GlobalVariable. The emitter recognizes these sampler reference types via `sub_1C2E890` and generates the structured PTX initializer. Texture and surface references use the simpler forms:

```ptx
.global .texref my_texture;
.global .surfref my_surface;
```

## End-to-End Pipeline

The complete texture/surface compilation pipeline spans five compiler phases:

| Phase | Function(s) | What Happens |
|---|---|---|
| **EDG Frontend** | `sub_72BA30` | Parses `__nv_tex_surf_handle_t` as built-in type; keyword 277 |
| **NVVM Builtin Lowering** | `sub_955A70` case `0x287` / `sub_954F10` | String-based dispatch constructs LLVM intrinsic names; red-black tree maps resolve builtin IDs to intrinsic IDs |
| **SelectionDAG Lowering** | `sub_33B0210` / `sub_33A4350` / `sub_33A3180` | 50+ texture intrinsic IDs become NVPTXISD DAG nodes; handle binding validated against GlobalVariable metadata |
| **Instruction Selection** | `sub_306A930` (52KB) | DAG nodes matched to `tex.*` / `suld.*` / `sust.*` machine instructions |
| **PTX Emission** | `sub_2156420` / `sub_21DD1A0` | `.texref`/`.surfref`/`.samplerref` globals emitted; image type validated; `NVPTXReplaceImageHandles` substitutes abstract handles |

## Architecture Considerations

Surface and texture operations are available on all SM architectures. However, the texture pipeline has evolved significantly:

- **All SM**: Basic texture fetch, surface read/write with clamp/trap/zero modes
- **SM 30+**: Unified texture mode via `__nv_tex_surf_handler` generic dispatch; `v2637=true` path in DAG lowering
- **SM 90+ (Hopper)**: Tensor memory accelerator (TMA) operations provide an alternative high-throughput path for bulk data movement, partially overlapping with texture/surface functionality but handled through separate builtins (IDs 411--412)

The 165 surface store builtins are registered unconditionally regardless of target SM. Architecture gating occurs at the PTX emission layer, not during builtin registration or lowering. The complex texture sample path (`intrinsic 0x91`) has an explicit SM feature gate via `sub_33CC4A0` that selects alternate instruction encodings for older architectures, with `sub_33A1E80` as the fallback for unsupported targets.

## Function Map

| Address | Function | Role |
|---|---|---|
| `sub_955A70` | NVVM builtin lowering dispatch | Main switch; case `0x287` handles `__nv_tex_surf_handler` |
| `sub_954F10` | Texture/surface sample handler | Red-black tree dispatch for IDs 302--309, 338--345, 395--402 |
| `sub_72BA30` | EDG keyword handler | Parses `__nv_tex_surf_handle_t` built-in type (keyword 277) |
| `sub_33B0210` | NVPTX intrinsic lowering | 343KB central dispatch; tex IDs 0x5D--0x8D, surf IDs 0x8E--0x90 |
| `sub_33A4350` | Texture fetch bulk handler | 50 consecutive intrinsic IDs for all tex1D/2D/3D/array variants |
| `sub_33A3180` | Surface read/write handler | 3 intrinsic IDs for surf1D/2D/3D read |
| `sub_33EB1C0` | Tex/surf sample DAG node builder | Creates memory-typed NVPTXISD sample nodes (opcode 47) |
| `sub_3409320` | Sampler state DAG node builder | Creates sampler state binding nodes |
| `sub_33AEC60` | Surface atomics handler | Intrinsic IDs 0x9C--0x9D |
| `sub_33AFBA0` | Surface special handler | Intrinsic ID 0x9E |
| `sub_306A930` | Texture/surface ISel | 52KB instruction selection for tex/suld/sust patterns |
| `sub_21DD1A0` | Image type validator | 16KB; validates `.tex`/`.suld`/`.sust`/`suq.` image types |
| `sub_21DBEA0` | NVPTXReplaceImageHandles | Replaces IR image handles with PTX `.texref`/`.surfref` |
| `sub_2156420` | Global variable emitter | 20KB; emits `.texref`/`.surfref`/`.samplerref` with initializers |
| `sub_21502D0` | Parameter list emitter | 22KB; emits `.param .texref`/`.surfref`/`.samplerref` in function signatures |
| `sub_2077400` | visitNVVMTexSurf | 20KB SelectionDAGBuilder extension for tex/surf handle lowering |
| `sub_BA8CA0` | NVVM intrinsic lookup | Resolves constructed intrinsic name string to LLVM function declaration |
| `sub_90A810` | Intrinsic table lookup | Resolves intrinsic ID to function declaration with type overloads |

## Cross-References

- [Builtin System Overview](./index.md) -- Hash table infrastructure and ID assignment
- [Atomics Builtins](./atomics.md) -- PTX inline asm generation pattern shared by surface stores
- [NVPTX Instruction Selection](../llvm/isel-patterns.md) -- ISel pattern matching context
- [SelectionDAG Lowering](../llvm/selectiondag.md) -- DAG node construction infrastructure
- [PTX Emission](../codegen/emission.md) -- Final instruction text generation
- [Address Spaces](../reference/address-spaces.md) -- Memory space qualifiers for tex/surf
