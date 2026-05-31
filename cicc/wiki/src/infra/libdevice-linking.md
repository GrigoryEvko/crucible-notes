# Libdevice Linking

NVIDIA embeds a complete copy of the `libdevice` math library -- 455,876 bytes of LLVM bitcode -- directly inside the cicc binary. This library provides GPU-optimized implementations of ~350 mathematical intrinsics (trigonometric, exponential, rounding, Bessel functions, error functions, type conversions, and integer utilities) that are linked into every CUDA compilation during the LNK pipeline stage. The linker (`sub_12C06E0`, 63KB) validates bitcode magic bytes, enforces the `nvptx64-` target triple prefix, checks NVVM IR version metadata for cross-release compatibility, and performs symbol-size matching across all modules before producing a single merged module. Two identical copies of the embedded bitcode exist in the binary -- one for each compilation path -- ensuring the library is always available without filesystem access.

Upstream LLVM has no equivalent of this embedded-library mechanism. Clang relies on external `libdevice.10.bc` files discovered through `--cuda-path` at driver level. NVIDIA's approach eliminates the file-lookup step entirely, making cicc self-contained: the entire math library ships inside the compiler binary itself.

| | |
|---|---|
| **Embedded size** | 455,876 bytes (445 KB) per copy |
| **Copies in binary** | 2: `unk_3EA0080` (Path A), `unk_420FD80` (Path B) |
| **Function count** | 352 defined (349 `__nv_*` public + 3 `__internal_*` helper) |
| **`__nvvm_reflect` calls** | 2,016 (architecture/precision dispatch) |
| **Target triple** | `nvptx64-nvidia-gpulibs` |
| **NVVM IR version** | `!nvvmir.version = !{i32 2, i32 0}` (always-compatible sentinel) |
| **Attribute group** | `#0 = { alwaysinline nounwind }` on all public functions |
| **Module linker** | `sub_12C06E0` (12 KB native; 2,154 lines decomp) |
| **Version checker** | `sub_12BFF60` (1.9 KB native; 362 lines decomp) |
| **Pipeline stage** | LNK (first stage, before OPT) |
| **Override** | `-nvvmir-library <path>` CLI flag substitutes an external file |
| **Version bypass** | `NVVM_IR_VER_CHK=0` disables IR version validation |

## Embedded Bitcode Layout

The cicc binary contains two byte-identical copies of the libdevice bitcode at different virtual addresses. Each compilation path uses its own copy, avoiding any shared-state coordination between Path A (nvcc-invoked) and Path B (standalone/LibNVVM):

```text
Binary offset         Path   Referenced by          Size
─────────────────────────────────────────────────────────────
unk_3EA0080           A      sub_905EE0 (9 KB native)   455,876 bytes
unk_420FD80           B      sub_1265970 (9 KB native)  455,876 bytes
```

Both copies contain identical LLVM bitcode with:

- **Data layout**: `e-i64:64-v16:16-v32:32-n16:32:64`
- **Target triple**: `nvptx64-nvidia-gpulibs` (note: `gpulibs`, not `cuda`)
- **Producer**: `clang version 3.8.0 (tags/RELEASE_380/final)` -- the bitcode was originally compiled with an ancient Clang but has been maintained through bitcode format upgrades across CUDA toolkit releases
- **Version metadata**: `!nvvmir.version = !{i32 2, i32 0}` -- this specific version tuple `(2, 0)` is hard-coded in the version checker as an always-compatible sentinel

The duplication exists because the two compilation paths (`sub_905EE0` for Path A, `sub_1265970` for Path B) are entirely independent code paths with no shared module state. Deduplicating the data would require introducing a shared pointer, which NVIDIA apparently considered not worth the ~445KB savings in a 60MB binary.

### Loading the Embedded Bitcode

In both paths, the embedded bitcode is passed to `sub_12BCB00` (the `nvvmCUAddModuleFromBuffer` API wrapper) with a hardcoded size constant:

```c
// Path A (sub_905EE0, line ~167):
v19 = sub_12BCB00(compilation_unit, &unk_3EA0080, 455876, 0);

// Path B (sub_1265970, line ~448):
v19 = sub_12BCB00(compilation_unit, &unk_420FD80, 455876, 0);
```

When the `-nvvmir-library <path>` flag is provided, the corresponding path opens the file, reads its contents into memory, and passes that buffer to `sub_12BCB00` instead of the embedded pointer. This override is used primarily for testing custom libdevice builds.

## Libdevice Function Inventory

The library defines 352 functions across 10 categories. All 349 public functions carry `alwaysinline nounwind` attributes, meaning they will be unconditionally inlined during the OPT stage after linking. Three internal helper functions (`__internal_trig_reduction_slowpathd`, `__internal_accurate_pow`, `__internal_lgamma_pos`) use `noinline nounwind` to avoid code size explosion in their callers.

| Category | Count | Examples |
|----------|------:|---------|
| Type conversions | 75 | `__nv_float2int_rn`, `__nv_double2ull_rz`, `__nv_int2float_rd`, `__nv_half2float` |
| Rounded arithmetic | 74 | `__nv_fmaf_rn`, `__nv_fdiv_rz`, `__nv_dsqrt_rd`, `__nv_dadd_ru`, `__nv_fmul_rn` |
| Trigonometric | 34 | `__nv_sinf`, `__nv_cos`, `__nv_tanf`, `__nv_asinf`, `__nv_atan2`, `__nv_sincospi` |
| Special functions | 30 | `__nv_erff`, `__nv_lgamma`, `__nv_j0`, `__nv_y1`, `__nv_cyl_bessel_i0`, `__nv_normcdf` |
| Roots and norms | 28 | `__nv_sqrtf`, `__nv_rsqrt`, `__nv_cbrt`, `__nv_hypot`, `__nv_norm3d`, `__nv_rnorm4d` |
| Exponential/logarithmic | 28 | `__nv_expf`, `__nv_log2`, `__nv_exp10`, `__nv_log1p`, `__nv_ldexp`, `__nv_frexp` |
| Integer utilities | 27 | `__nv_clz`, `__nv_popc`, `__nv_brev`, `__nv_mulhi`, `__nv_abs`, `__nv_byte_perm` |
| Float utilities | 20 | `__nv_fabsf`, `__nv_fminf`, `__nv_copysign`, `__nv_fmod`, `__nv_nextafter`, `__nv_nan` |
| Rounding | 14 | `__nv_floorf`, `__nv_ceil`, `__nv_truncf`, `__nv_roundf`, `__nv_nearbyintf`, `__nv_rint` |
| Classification | 11 | `__nv_isinff`, `__nv_isnand`, `__nv_isfinited`, `__nv_signbitf`, `__nv_ilogb`, `__nv_logb` |
| Internal helpers | 3 | `__internal_trig_reduction_slowpathd`, `__internal_accurate_pow`, `__internal_lgamma_pos` |

Every public function body contains calls to `@__nvvm_reflect` with query strings (`__CUDA_FTZ`, `__CUDA_ARCH`, `__CUDA_PREC_SQRT`) that are resolved by the NVVMReflect pass during optimization. This is how the same bitcode adapts to different precision modes and SM architectures -- see [NVVMReflect](../passes/nvvm-reflect.md) for details on the reflection mechanism. The 2,016 reflect calls across 352 functions means an average of ~5.7 architecture/precision branch points per function.

### Struct Types

The bitcode defines five aggregate types used by multi-return functions:

```llvm
%struct.uint2                  = type { i32, i32 }
%struct.float2                 = type { float, float }
%struct.trig_reduction_return  = type { double, i32 }
%struct.ulonglong2             = type { i64, i64 }
%struct.double2                = type { double, double }
```

`trig_reduction_return` is used by the internal trigonometric range reduction helper. The `float2`/`double2` types appear in `sincos`/`sincospi` which return both sine and cosine through output pointers.

### Constant Tables

The bitcode contains precomputed coefficient tables in address space 1 (global memory):

| Global | Type | Purpose |
|--------|------|---------|
| `@__cudart_i2opi_f` | `[6 x i32]` | Float-precision inverse-of-pi table for trig reduction |
| `@__cudart_i2opi_d` | `[18 x i64]` | Double-precision inverse-of-pi table for trig reduction |
| `@__cudart_sin_cos_coeffs` | `[16 x double]` | Chebyshev coefficients for sin/cos polynomial approximation |

## Module Linker Algorithm

`sub_12C06E0` (12 KB native) is the central module linker that operates during the LNK pipeline stage. It receives a list of user modules and a list of builtin modules (which includes libdevice), validates them, and produces a single merged LLVM module. The algorithm proceeds in six phases:

### Phase A: Module Iteration and Bitcode Validation

For each module in the input list (from `a1[0]` to `a1[1]`, stepping by 4 qwords per entry), the linker:

1. Opens and reads the module data via `sub_16C2450`
2. Validates LLVM bitcode magic bytes -- accepts two formats:
   - Raw bitcode: bytes `0xDE 0xC0 0x17 0x0B` (little-endian `0x0B17C0DE`)
   - Bitcode wrapper: bytes `0x42 0x43 0xC0 0xDE` (ASCII "BC" prefix)
3. Determines the buffer name (falls back to `"Unknown buffer"` if the vtable function is `sub_12BCB10`)
4. Parses bitcode into an LLVM Module via `sub_15099C0`

```c
for each entry in modules[a1[0] .. a1[1]]:
    buffer = open_and_read(entry.data, entry.size, entry.name)
    magic = read_4_bytes(buffer)
    if magic != 0x0B17C0DE and magic != 0xDEC04342:
        *error_code = 9   // invalid bitcode
        return NULL
    name = (entry.vtable_func == sub_12BCB10)
           ? "Unknown buffer"
           : entry.vtable_func(entry)
    module = parse_bitcode(buffer, llvm_ctx, name)
```

### Phase B: Triple Validation

After parsing all modules, the linker enforces that every module's target triple starts with `nvptx64-`. The comparison uses a prefix match against the global string at `off_4CD49B0`:

```c
for each parsed_module:
    triple = get_triple(parsed_module)   // offset +240
    if triple.length == 0:
        error: "Module does not contain a triple, should be 'nvptx64-'"
        *error_code = 9
    else if !starts_with(triple, "nvptx64-"):
        error: "<module_name>: Module does not contain a triple, should be 'nvptx64-'"
        *error_code = 9
```

The libdevice bitcode has triple `nvptx64-nvidia-gpulibs`, which passes this prefix check. User modules typically have `nvptx64-nvidia-cuda`.

### Phase C: IR Version Check

For each module, the linker calls `sub_12BFF60` (the version checker -- see next section). If the check fails, the linker emits a diagnostic and returns error code 3:

```c
for each parsed_module:
    result = nvvm_ir_version_check(modules, parsed_module, flags)  // sub_12BFF60
    if result != 0:
        error: "<name>: error: incompatible IR detected. "
               "Possible mix of compiler/IR from different releases."
        *error_code = 3
        return NULL
```

### Phase D: Single-Module Fast Path

When only one module exists (no linking needed), the linker returns it directly via `sub_1C3DFC0` without invoking any linking machinery. This fast path avoids the overhead of LLVM's `Linker::linkModules` for the common case of a single translation unit without libdevice.

### Phase E: Multi-Module User Linking

For N > 1 user modules, the linker:

1. Selects one module as the "primary" (index `v57`)
2. Copies the primary module's triple and data layout to all secondary modules (ensuring consistency)
3. Calls `sub_12F5610` -- NVIDIA's wrapper around LLVM's `Linker::linkModules` -- to merge all user modules into a single module

```c
if module_count > 1:
    primary = modules[v57]
    for each secondary in modules where index != v57:
        set_triple(secondary, get_triple(primary))
        set_data_layout(secondary, get_data_layout(primary))
    result = LinkModules(&modules, linking_state, &error_str, &warnings, options)
    if result != 0:
        error: "<module_name>: link error: <details>"
        *error_code = 9
```

### Phase F: Builtin Linking

After user modules are merged, the linker processes builtin modules from `a1[3]` to `a1[4]` (this is where libdevice lives). Each builtin module goes through the same bitcode validation and parsing as user modules, then is linked into the main module using `sub_1CCEBE0` -- a different linking function than the user-module linker, likely `Linker::linkModules` with `Linker::OverrideFromSrc` flags for builtin definitions:

```c
for each builtin in modules[a1[3] .. a1[4]]:
    validate_and_parse(builtin)
    set_triple(builtin, get_triple(main_module))
    result = LinkBuiltinModule(main_module, builtin, &error_string)
    if result != 0:
        error: "builtins: link error: <details>"
        // continues -- does not abort on builtin link failure
    post_link_cleanup(main_module, target_features)
```

The post-link cleanup sequence (`sub_1611EE0` through `sub_160FE50`) configures target features on the merged module and finalizes symbol resolution.

### Phase G: Symbol Size Matching

The final validation phase walks every global symbol in the linked module and checks that declarations and definitions agree on type sizes. The linker maintains a binary search tree keyed by symbol name and computes type sizes using a recursive size calculator:

| Type code | Type | Size formula |
|-----------|------|-------------|
| 1 | half | 16 bits |
| 2 | float | 32 bits |
| 3, 9 | double, i64 | 64 bits |
| 4 | fp80 | 80 bits |
| 5, 6 | fp128 | 128 bits |
| 7 | pointer | 8 * pointer_size |
| 0xB | integer | `bits >> 8` |
| 0xD | struct | sum of member sizes |
| 0xE | array | `alignment * count * ceil(element_bits / (8 * alignment))` |
| 0xF | named type | resolved recursively |
| 0x10 | vector | element_size * count |

```c
for each global_symbol in linked_module:
    name = get_name(global_symbol)
    if name in size_tree:
        existing_size = size_tree[name].size
        new_size = compute_type_size(global_symbol.type)
        if existing_size != new_size:
            error: "Size does not match for <name> in <module_A> "
                   "with size X specified in <module_B> with size Y."
            size_mismatch = true
    else:
        size_tree.insert(name, compute_type_size(global_symbol.type))
if size_mismatch:
    *error_code = 9
```

## Triple and Version Validation

### NVVM IR Version Checker (`sub_12BFF60`)

The version checker validates the `nvvmir.version` metadata node that every NVVM-produced bitcode module carries. It ensures that modules compiled by different CUDA toolkit versions are not accidentally mixed.

**Metadata lookup**: The checker searches for two named metadata nodes:

1. `"nvvmir.version"` -- the IR version tuple
2. `"llvm.dbg.cu"` -- debug compile unit (presence indicates debug info exists)

Both are looked up via `sub_1632310` (named metadata search on the module).

**Version tuple format**: The metadata node contains either 2 or 4 constant integer operands:

| Format | Operands | Meaning |
|--------|----------|---------|
| 2-element | `{major, minor}` | IR version only |
| 4-element | `{major, minor, dbg_major, dbg_minor}` | IR version + debug IR version |

**Compatibility check**: For the IR version, `sub_12BDA30` performs the actual comparison. The special case `(major=2, minor=0)` always passes -- this is exactly the version carried by the embedded libdevice, ensuring it is compatible with any user module regardless of toolkit version.

For the debug version, `sub_12BD890` checks compatibility with a similar special case: `(debug_major=3, debug_minor<=2)` always passes.

**Unique node deduplication**: The checker builds a hash set of unique metadata nodes using the standard DenseMap infrastructure with NVVM-layer sentinels (-8 / -16). See [Hash Table and Collection Infrastructure](../infra/hash-infrastructure.md) for the hash function and probing strategy. This deduplication handles the case where multiple source files within a compilation unit carry identical version metadata -- each unique version is checked exactly once.

**Final gate**: If debug info is present in the module, the debug mode flag is set, but no debug version was validated (because the metadata lacked elements 2-3), the checker returns 3 (incompatible). This catches the case where a debug-compiled user module is linked against a non-debug library that lacks debug version metadata.

## Symbol Resolution During LNK

The LNK stage processes libdevice functions through LLVM's standard symbol resolution mechanism. Because all 349 public libdevice functions carry the `alwaysinline` attribute, the resolution and inlining follow a specific sequence:

1. **Declaration matching**: User code that calls `__nv_sinf(x)` contains an external declaration `declare float @__nv_sinf(float)`. The linker resolves this declaration against the `define float @__nv_sinf(float)` in libdevice.

2. **`__nvvm_reflect` remains unresolved**: After linking, libdevice function bodies contain calls to `@__nvvm_reflect` which are still unresolved declarations. These are handled during the OPT stage by the NVVMReflect pass, not during linking.

3. **Dead function elimination**: Functions from libdevice that are never called by user code are eliminated by `GlobalDCE` during the OPT stage. Since libdevice provides 352 functions but a typical kernel uses only a handful, the vast majority are stripped.

4. **`alwaysinline` enforcement**: During the OPT stage, the `AlwaysInliner` pass processes all libdevice functions. After inlining, the original function bodies become dead (no remaining callers) and are removed by subsequent DCE.

The net effect: a kernel calling `__nv_sinf` ends up with the sinf implementation inlined directly into the kernel body, with `__nvvm_reflect` calls already resolved to constants by NVVMReflect, and all unused branches from precision/architecture dispatch eliminated by SimplifyCFG.

## Constant Folding Interaction

The constant folding engine (`sub_14D90D0`, 27KB) has special knowledge of libdevice functions. When a libdevice intrinsic is called with constant arguments, the fold eligibility checker determines whether the call can be evaluated at compile time -- *before* the libdevice function is inlined.

This creates an important ordering constraint:

```text
LNK stage:  link libdevice → user module now has __nv_sinf definitions
OPT stage:  NVVMReflect  → resolve __CUDA_FTZ, __CUDA_ARCH queries
            ConstantFold → fold __nv_sinf(0.0) → 0.0 (if eligible)
            AlwaysInline → inline remaining __nv_sinf calls
            SimplifyCFG  → remove dead reflect branches
            GlobalDCE    → remove unused libdevice functions
```

The fold eligibility checker (`sub_14D90D0`) uses three dispatch mechanisms to identify foldable functions:

**LLVM intrinsic ID switch** (IDs 0-211): Covers standard LLVM intrinsics like `llvm.sin`, `llvm.cos`, `llvm.sqrt`, `llvm.fma`, `llvm.floor`, `llvm.ceil`, `llvm.exp`, `llvm.log`, `llvm.pow`, `llvm.fabs`, `llvm.bswap`, `llvm.ctlz`, `llvm.ctpop`, and overflow arithmetic.

**NVVM intrinsic ID ranges** (IDs > 211): Covers NVIDIA-specific intrinsics organized as binary-search ranges with bitmask dispatch:

| Range | IDs | Examples |
|-------|-----|---------|
| 0xEB4-0xEE3 | 3764-3811 | `nvvm.ceil.f`, `nvvm.ctlz.i`, `nvvm.cos.approx.ftz.f` |
| 0xF1E-0xF72 | 3870-3954 | `nvvm.exp2.approx`, `nvvm.fabs.f`, `nvvm.floor.f`, `nvvm.sqrt.f` |
| 0xFE8-0xFEA | 4072-4074 | `nvvm.sin.approx.ftz.f` and similar |
| 0x1012-0x104C | 4114-4172 | `nvvm.max.i`, `nvvm.min.ui`, `nvvm.min.ll` |
| 0x1086-0x1087 | 4230-4231 | `nvvm.mul.hi.*` |
| 0x117B-0x1184 | 4475-4484 | `nvvm.sqrt.rn.d`, `nvvm.sqrt.approx.ftz.f` |
| 0x1C80-0x1CAC | 7296-7340 | `nvvm.fmax.f`, `nvvm.fmin.ftz.nan.f` |

**Name-based matching** (ID = 0): When the call target is not a recognized LLVM or NVVM intrinsic, the checker falls back to string matching on the function name. It dispatches on the first character, then uses DWORD integer comparisons for 4-byte names and `memcmp` for longer names:

```text
Foldable C library names:
  sin, sinf, cos, cosf, tan, tanf, acos, acosf, asin, asinf,
  atan, atanf, atan2, atan2f, ceil, ceilf, cosh, coshf,
  exp, expf, exp2, exp2f, fabs, fabsf, floor, floorf,
  fmod, fmodf, log, logf, log10, log10f, pow, powf,
  round, roundf, sinh, sinhf, sqrt, sqrtf, tanh, tanhf
```

**Convergent gate**: Before any folding, the checker verifies that the callee does **not** carry the `convergent` attribute (kind 0x34). Convergent functions have warp-synchronous semantics and must not be speculatively constant-folded, even if all arguments are constants.

## Configuration

### Environment Variables

| Variable | Effect |
|----------|--------|
| `NVVM_IR_VER_CHK` | Set to `"0"` to disable IR version validation. Any other value or unset = enabled (default). Checked in `sub_12BFF60` at `0x12BFF60` and in the duplicate verifier at `0x2259720`. |

### CLI Flags

| Flag | Effect |
|------|--------|
| `-nvvmir-library <path>` | Override the embedded libdevice with an external bitcode file. The file is opened, read into memory, and passed to the linker in place of the embedded `unk_3EA0080`/`unk_420FD80` pointer. |
| `-opt` / `-llc` | When passed as the first extra argument, skips builtin linking entirely (jumps past the libdevice linking code to direct pipeline stage invocation). |
| `-keep` | Preserves `.lnk.bc` intermediate file showing the linked module (user + libdevice) before optimization. |

### Intermediate Files

When `-keep` is active, the LNK stage serializes its output to a `.lnk.bc` file alongside the input:

```text
input.cu  →  input.lnk.bc  (linked: user + libdevice)
          →  input.opt.bc  (optimized: after OPT stage)
          →  input.ptx     (final: after LLC stage)
```

The `.lnk.bc` file is useful for verifying which libdevice functions survived linking and how `__nvvm_reflect` calls appear before the NVVMReflect pass resolves them.

## Function Map

| Address | Size | Role |
|---|---|---|
| `sub_12C06E0` | 63KB | Main bitcode linker: validates magic, triple, version; links user modules, then builtins |
| `sub_12BFF60` | 9KB | Reads `nvvmir.version` metadata, checks compatibility via `sub_12BDA30`/`sub_12BD890` |
| `sub_12BDA30` | ~2KB | IR version compatibility predicate (special-cases `{2,0}` as always-compatible) |
| `sub_12BD890` | ~2KB | Debug IR version compatibility predicate (special-cases `{3, <=2}`) |
| `sub_12C35D0` | 41KB | 4-stage pipeline driver; calls `sub_12C06E0` during LNK stage |
| `sub_905EE0` | 43KB | Path A pipeline driver; references `unk_3EA0080` for embedded libdevice |
| `sub_1265970` | 48KB | Path B pipeline driver; references `unk_420FD80` for embedded libdevice |
| `sub_12BCB00` | ~1KB | API wrapper that adds a bitcode buffer to the compilation unit (string-confirmed: `"nvvmCUAddModuleFromBuffer"`) |
| `sub_12BC0F0` | 3KB | Resolves LibNVVM API function pointers by hash ID |
| `sub_15099C0` | ~8KB | LLVM bitcode parser entry point |
| `sub_1CCEBE0` | ~4KB | Links a single builtin module into the main module (`Linker::linkModules` with `OverrideFromSrc` `[MEDIUM confidence]` -- inferred from the override-from-source semantics of builtin linking and the 4KB size matching a thin wrapper around LLVM's linker API, but no diagnostic string confirms the exact LLVM API call) |
| `sub_12F5610` | ~4KB | Links multiple user modules (`Linker::linkModules` `[MEDIUM confidence]` -- same reasoning as above; wrapper size and call pattern match, but unconfirmed by string evidence) |
| `sub_14D90D0` | 27KB | Constant-fold eligibility checker for math intrinsics |
| `unk_3EA0080` | 455,876B | Embedded libdevice (Path A): raw LLVM bitcode blob |
| `unk_420FD80` | 455,876B | Embedded libdevice (Path B): raw LLVM bitcode blob (identical copy) |

## Reimplementation Checklist

1. **Embedded bitcode storage and loading.** Embed the libdevice bitcode blob (455,876 bytes) directly in the compiler binary, provide two independent copies for dual-path compilation (Path A / Path B), and implement the `nvvmCUAddModuleFromBuffer` API wrapper to load the embedded blob or an external override file via `-nvvmir-library`.
2. **Bitcode magic validation.** Accept two bitcode formats: raw bitcode (`0xDE 0xC0 0x17 0x0B`, little-endian `0x0B17C0DE`) and bitcode wrapper (`0x42 0x43 0xC0 0xDE`, ASCII "BC" prefix). Reject anything else with error code 9.
3. **Target triple and IR version validation.** Enforce `nvptx64-` prefix on all module triples. Implement the NVVM IR version checker that reads `nvvmir.version` metadata (2-element or 4-element tuples), special-cases version `{2,0}` as always-compatible (the libdevice sentinel), and checks debug IR version compatibility for `{3, <=2}`.
4. **Multi-module linking pipeline.** Implement the six-phase linker: (A) module iteration with bitcode validation, (B) triple validation, (C) IR version check, (D) single-module fast path, (E) multi-module user linking with primary module selection and triple/data-layout propagation, (F) builtin linking with `OverrideFromSrc` semantics.
5. **Symbol size matching.** Walk all global symbols in the linked module, compute type sizes recursively (handling half/float/double/pointer/integer/struct/array/vector types), and verify that declarations and definitions agree on type sizes using a binary search tree keyed by symbol name.
6. **Constant folding integration.** Implement the fold eligibility checker for libdevice functions with three dispatch mechanisms (LLVM intrinsic ID switch for IDs 0--211, NVVM intrinsic ID ranges for IDs >211, name-based matching for C library names), gated by the convergent attribute check to prevent folding warp-synchronous functions.

## Cross-References

- [Entry Point & CLI](../pipeline/entry.md) -- dual-path architecture, `-nvvmir-library` flag handling
- [NVVMReflect](../passes/nvvm-reflect.md) -- resolution of `__nvvm_reflect` calls embedded in libdevice functions
- [Optimizer Pipeline](../pipeline/optimizer.md) -- OPT stage where inlining and DCE process linked libdevice
- [Environment Variables](../config/env-vars.md) -- `NVVM_IR_VER_CHK` documentation
- [Bitcode I/O](../infra/bitcode-io.md) -- bitcode reader/writer infrastructure used by the linker
