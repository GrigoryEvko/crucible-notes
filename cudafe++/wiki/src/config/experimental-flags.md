# Experimental and Version-Gated Flags

cudafe++ gates several categories of CUDA language features behind flags that nvcc manages automatically. Users interact with these through nvcc options like `--expt-extended-lambda` and `--expt-relaxed-constexpr`; nvcc translates these into the internal cudafe++ flags `--extended-lambda` and `--relaxed_constexpr` before invocation. A third category, C++ standard version gating, controls which language-level features affect the CUDA compilation pipeline. Two additional flags (`--default-device`, `--no-device-int128`/`--no-device-float128`) tune device code semantics without the "experimental" label.

This page documents the internal mechanism of each flag: the global variable it sets, every code path it unlocks, the diagnostics it suppresses or enables, and the compile-time cost of enabling it.

## Flag Summary

| nvcc Flag | cudafe++ Internal Flag | Flag ID | Global Variable | Default | Effect |
|---|---|---|---|---|---|
| `--expt-extended-lambda` | `--extended-lambda` | 79\* | `dword_106BF38` | 0 | Enable entire extended lambda wrapper infrastructure |
| `--expt-relaxed-constexpr` | `--relaxed_constexpr` | 104 | `dword_106BF40` | 0 | Allow constexpr cross-space calls |
| `-std=c++NN` | `--c++NN` / `set_flag` | -- | `dword_126EF68` | 199711 | Gate C++ standard features |
| (JIT mode) | `--default-device` | \*\* | -- | 0 | Change unannotated default to `__device__` |
| `--no-device-int128` | `--no-device-int128` | 52 | -- | 0 | Disable `__int128` in device code |
| `--no-device-float128` | `--no-device-float128` | 53 | -- | 0 | Disable `__float128`/`_Float128` in device code |

\* The extended-lambda flag is registered as flag 79 (`disable_ext_lambda_cache` is a separate flag at that slot in some reports; the exact case_id for the flag parsed as `extended-lambda` is in the CUDA-specific range 47--89 but the individual case within the grouped 47--53 block is not fully disambiguated). The flag string `"extended-lambda"` is at binary address `0x836410`, referenced from `sub_452010` (init_command_line_flags).

\*\* The `--default-device` flag is not in the standard numbered flag catalog (1--275). It is registered through one of the 7 inline-registered paired flags or the `set_flag`/`clear_flag` table (`off_D47CE0`). Its string literal appears in four JIT error messages in the binary.

## `--extended-lambda` (dword_106BF38)

This is the single most impactful experimental flag in cudafe++. It enables the entire extended lambda subsystem -- approximately 40 functions in `nv_transforms.c`, 2,100 lines of lambda scanning in `cmd_line.c`, 17 steps of preamble text emission, and per-lambda wrapper generation in the backend. Without it, CUDA lambdas annotated with `__device__` or `__host__ __device__` are rejected outright.

### What It Enables

When `dword_106BF38 != 0`, the following subsystems activate:

**1. Lambda scanning (sub_447930, 2,113 lines)**

The 7-phase `scan_lambda` function performs full CUDA validation on every lambda expression. Phase 4 checks all 35+ restriction categories documented in the [restrictions page](../lambda/restrictions.md). Without the flag, phase 4 early-exits and emits error 3612 instead.

**2. Preamble injection (sub_4864F0 + sub_6BCC20)**

When the backend encounters a type declaration for the sentinel `__nv_lambda_preheader_injection`, three conditions must all be true for the preamble to fire:

```c
// sub_4864F0 trigger conditions:
if ((entity_bits[-8] & 0x10) != 0      // marker bit set
    && dword_106BF38 != 0               // --extended-lambda enabled
    && name_matches_sentinel)           // 30-byte name comparison
{
    sub_6BCC20(emit_func);              // emit ~10-50 KB of template text
}
```

The master emitter (`sub_6BCC20`) produces the complete lambda wrapper infrastructure as inline C++ text injected into the `.int.c` output. The 17-step emission sequence generates:

| Step | Output | Purpose |
|---|---|---|
| 1 | `__NV_LAMBDA_WRAPPER_HELPER`, `__nvdl_remove_ref`, `__nvdl_remove_const` | Utility macros and type traits |
| 2 | `__nv_dl_tag` | Device lambda tag type |
| 3 | Array capture helpers (dim 2--8) | N-dimensional array forwarding via `sub_6BC290` |
| 4 | Primary `__nv_dl_wrapper_t` + zero-capture specialization | Device lambda wrapper template |
| 5 | `__nv_dl_trailing_return_tag` + zero-capture specialization | Trailing return type support |
| 6 | Device bitmap scan | One `sub_6BB790` call per set bit in `unk_1286980` |
| 7 | `__nv_hdl_helper` (anonymous namespace, 4 static function pointers) | Host-device lambda dispatch helper |
| 8 | Primary `__nv_hdl_wrapper_t` with `static_assert` | Host-device wrapper template |
| 9 | HD bitmap scan | Four calls per set bit in `unk_1286900` (const x mutable x 2 helpers) |
| 10 | `__nv_hdl_helper_trait_outer` | Deduction helper traits |
| 11 | C++17 `noexcept` variants | Conditional on `dword_126E270` (see [C++ version gating](#c-standard-version-gating-dword_126ef68)) |
| 12 | `__nv_hdl_create_wrapper_t` | Factory for HD wrappers |
| 13 | `__nv_lambda_trait_remove_const/volatile/cv` | CV-qualifier removal traits |
| 14 | `__nv_extended_device_lambda_trait_helper` + detection macro | Device lambda type detection |
| 15 | `__nv_lambda_trait_remove_dl_wrapper` | Unwrapper trait |
| 16 | Trailing-return detection trait + macro | Type introspection |
| 17 | HD detection trait + macro | Host-device lambda type detection |

**3. 1024-bit capture bitmaps**

Two bitmaps track which capture counts have been observed during parsing:

| Bitmap | Address | Scope | Bits Used |
|---|---|---|---|
| Device | `unk_1286980` | 128 bytes (1024 bits) | Bit N = capture count N seen in a `__device__` lambda |
| Host-device | `unk_1286900` | 128 bytes (1024 bits) | Bit N = capture count N seen in an HD lambda |

`sub_6BCBF0` registers a capture count by setting the corresponding bit. `sub_6BCBC0` resets both bitmaps to zero between translation units. The maximum representable capture count is 1023 (bit 0 is reserved for the primary template in the device path; the HD path uses bit 0). Error 3595 fires when capture count exceeds 1022 (`v33 > 0x3FE`).

**4. Per-lambda wrapper generation (sub_47B890, 336 lines)**

During backend code generation, `gen_lambda` produces the per-lambda wrapper specialization for each extended lambda encountered. This runs in the `gen_template` dispatcher (`sub_47ECC0`).

**5. Extended lambda capture type generation (sub_46E640, ~400 lines)**

`nv_gen_extended_lambda_capture_types` generates explicit type declarations for captured variables, enabling the closure type to be serialized across host/device boundaries.

### What Happens Without It

When `dword_106BF38 == 0`, any lambda with `__host__` or `__device__` annotations triggers error 3612:

```
error #20155-D: __host__ or __device__ annotation on lambda requires --extended-lambda nvcc flag
```

Additionally, the `.int.c` header emits hardcoded false macros (from `sub_489000`):

```c
#define __nv_is_extended_device_lambda_closure_type(X) false
#define __nv_is_extended_host_device_lambda_closure_type(X) false
#define __nv_is_extended_device_lambda_with_preserved_return_type(X) false
```

These definitions ensure that code using the detection macros compiles without error but reports that no extended lambdas exist.

### Compile-Time Cost

Enabling `--extended-lambda` has measurable compile-time impact:

- **Fixed overhead**: ~10 KB of injected template text (steps 1--5, 7--8, 10--17) emitted for every translation unit, regardless of how many lambdas appear
- **Variable per capture count**: ~0.8 KB per distinct device lambda capture count, ~6 KB per distinct HD capture count (the HD path emits 4 specializations per bit: const non-mutable, const mutable, non-const non-mutable, non-const mutable)
- **Typical TU with 3--5 distinct capture counts**: 30--50 KB of additional `.int.c` text
- **Template instantiation load**: The wrapper templates use deep SFINAE patterns; the host compiler (gcc/clang/MSVC) must instantiate these for every extended lambda in the TU
- **Lambda scanning**: The 2,113-line `scan_lambda` function performs full restriction validation on every lambda expression, adding O(N) per-lambda overhead

The cost is proportional to the number of distinct capture counts, not the total number of lambdas. Two `__device__` lambdas each capturing 3 variables share a single wrapper specialization.

### Related Error Codes

All 35+ extended lambda error codes (3590--3691) are documented in [lambda/restrictions.md](../lambda/restrictions.md). Key errors specific to the flag gate:

| Error | Display | Tag | Condition |
|---|---|---|---|
| 3612 | 20155-D | `extended_lambda_disallowed` | Lambda has `__host__`/`__device__` annotation but `dword_106BF38 == 0` |
| 3595 | 20138-D | `extended_lambda_too_many_captures` | Capture count > 1023 |
| 3590 | 20133-D | `extended_lambda_multiple_parent` | Multiple `__nv_parent` pragmas |

## `--expt-relaxed-constexpr` (dword_106BF40)

This flag relaxes cross-execution-space calling rules for `constexpr` functions. Without it, a `constexpr __device__` function cannot be called from a `__host__` function and vice versa, even though `constexpr` functions are evaluated at compile time on the host regardless of their execution space annotation.

### Flag Registration

Registered as flag ID 104 (`relaxed_constexpr`) in the CUDA-specific flag range. The `--expt-relaxed-constexpr` nvcc flag is translated to `--relaxed_constexpr` before passing to cudafe++. The flag sets `dword_106BF40` to 1.

Note: Despite the W066 report labeling this global `lambda_host_device_mode`, the decompiled code shows it is checked in two distinct contexts: cross-space call validation (`sub_505720`) and extended lambda device qualification (`sub_6BC680`). The variable name reflects its role in relaxing constexpr constraints, not lambda-specific behavior. It affects lambda behavior only in the specific case of `is_device_or_extended_device_lambda` (see below).

### What It Relaxes

The flag modifies behavior in two code paths:

**1. Cross-space call checking (sub_505720)**

In `check_cross_execution_space_call`, when the caller is a `__device__`-only function and the callee has bit 2 set at offset +177 (explicit `__device__` annotation), the checker tests `dword_106BF40`:

```c
// sub_505720, caller is __device__ or __global__, callee is constexpr __host__:
if ((callee[177] & 0x02) != 0) {     // callee has explicit execution space
    if (dword_106BF40) {               // --expt-relaxed-constexpr
        // skip error, allow the call
        return;
    }
}
```

Without the flag, this path falls through to emit one of the 6 constexpr-specific cross-space errors.

**2. Device lambda qualification (sub_6BC680)**

In `is_device_or_extended_device_lambda`, when an entity has `__device__` annotation (bit 177|2) but NOT the extended lambda bit (bit 177|4), the function returns `dword_106BF40 != 0`:

```c
// sub_6BC680 (decompiled):
bool is_device_or_extended_device_lambda(entity* a1) {
    if ((a1->byte_177 & 0x02) != 0) {       // has __device__
        if ((a1->byte_177 & 0x04) == 0) {    // NOT extended lambda
            return dword_106BF40 != 0;        // relaxed constexpr allows it
        }
        return true;
    }
    return false;
}
```

This means `--expt-relaxed-constexpr` allows certain `__device__` lambdas to be treated as extended device lambdas even without the `--extended-lambda` flag, but only in the specific context of device lambda type checking.

### The 6 Error Messages It Suppresses

When `dword_106BF40 == 0` and a constexpr function call crosses execution spaces, one of these 6 error messages is emitted. Each message explicitly suggests the flag as a workaround:

| # | Caller Space | Callee Space | Error Message |
|---|---|---|---|
| 1 | `__host__ __device__` | constexpr `__device__` | "calling a constexpr \_\_device\_\_ function(%sq1) from a \_\_host\_\_ \_\_device\_\_ function(%sq2) is not allowed. The experimental flag '--expt-relaxed-constexpr' can be used to allow this." |
| 2 | `__host__` | constexpr `__device__` | "calling a constexpr \_\_device\_\_ function(%sq1) from a \_\_host\_\_ function(%sq2) is not allowed. ..." |
| 3 | `__host__ __device__` | constexpr `__host__` | "calling a constexpr \_\_host\_\_ function(%sq1) from a \_\_host\_\_ \_\_device\_\_ function(%sq2) is not allowed. ..." |
| 4 | `__host__ __device__` | constexpr `__host__` | "calling a constexpr \_\_host\_\_ function from a \_\_host\_\_ \_\_device\_\_ function is not allowed. ..." (no entity names -- edge case for unresolved functions) |
| 5 | `__device__` | constexpr `__host__` | "calling a constexpr \_\_host\_\_ function(%sq1) from a \_\_device\_\_ function(%sq2) is not allowed. ..." |
| 6 | `__global__` | constexpr `__host__` | "calling a constexpr \_\_host\_\_ function(%sq1) from a \_\_global\_\_ function(%sq2) is not allowed. ..." |

The `%sq1` and `%sq2` format specifiers are cudafe++'s diagnostic format for qualified entity names (see [diagnostics/format-specifiers.md](../diagnostics/format-specifiers.md)).

### Why It Is Experimental

The flag is labeled "experimental" because enabling it can produce silent runtime errors when:

1. A `constexpr` function has **different behavior on host vs device** due to `#ifdef __CUDA_ARCH__` guards or host/device-specific intrinsics. The compiler evaluates `constexpr` functions on the host during compilation, but with the flag enabled, a `constexpr __device__` function might be evaluated on the host where `__CUDA_ARCH__` is not defined, producing a different constant value than the programmer expects for device code.

2. A `constexpr __host__` function references host-only APIs (file I/O, system calls, host-specific math libraries). With relaxed constexpr, this function can be called from a `__device__` context. If the call is not resolved at compile time (not actually evaluated as a constant expression), the linker or runtime will fail with an obscure error rather than the clear cudafe++ diagnostic.

3. The relaxation applies **globally** -- there is no per-function opt-in. Once enabled, all constexpr cross-space calls are permitted, making it impossible to catch genuinely incorrect calls alongside intentionally relaxed ones.

The related diagnostic tag is `cl_relaxed_constexpr_requires_bool` (at binary address `0x853640`), which indicates there was at some point a stricter validation that the flag's value must be boolean.

### Interaction with Other Globals

The `dword_106BF40` flag interacts with the cross-space checking infrastructure controlled by `dword_106BFD0` (device_registration) and `dword_106BFCC` (constant_registration). When `dword_106BF40` is set AND the current routine is in device scope (`+182 & 0x30 == 0x20`) AND the routine has `__device__` annotation (`+177 bit 1`), the cross-space reference check in `record_symbol_reference_full` (`sub_72A650`/`sub_72B510`) skips the error entirely.

## C++ Standard Version Gating (dword_126EF68)

The global variable `dword_126EF68` holds the C++ (or C) standard version as an integer matching the `__cplusplus` or `__STDC_VERSION__` predefined macro value. This is set during CLI parsing and controls feature gating throughout the frontend.

### Version Values

| Standard | `dword_126EF68` Value | nvcc Flag |
|---|---|---|
| C++98/03 | 199711 | `-std=c++03` |
| C++11 | 201103 | `-std=c++11` |
| C++14 | 201402 | `-std=c++14` |
| C++17 | 201703 | `-std=c++17` |
| C++20 | 202002 | `-std=c++20` |
| C++23 | 202302 | `-std=c++23` |

C standard values are also stored here when compiling C code:

| Standard | `dword_126EF68` Value |
|---|---|
| K&R | (triggers `set_c_mode(1)` instead) |
| C89 | 198912 |
| C99 | 199901 |
| C11 | 201112 |
| C17 | 201710 |
| C23 | 202311 |

### How Version Gating Works

Throughout the frontend, `dword_126EF68` is compared against threshold values to enable or disable features. The comparison is always `>=` or `>` against the version number. Examples from the binary:

**List initialization (sub_6D7DE0, overload.c)**: The 2,119-line list initialization function checks `dword_126EF68 >= 201103` before enabling C++11 brace-enclosed initializer semantics.

**Operator overloading (sub_6E7310, overload.c)**: Checks `dword_126EF68 >= 201703` for C++17 features like class template argument deduction in operator resolution.

**Preprocessor directives (sub_6FEDD0, preproc.c)**: Checks `dword_126EF68 >= 202301` for `#elifdef`/`#elifndef` support (C++23 feature).

**Byte ordering in .int.c output (sub_489000)**: Sets `byte_10657F4` based on:
```c
if (dword_126EFB4 == 2)              // CUDA mode
    byte_10657F4 = (dword_126EFB0 != 0);
else if (dword_126EF68 <= 199900)    // pre-C99
    byte_10657F4 = (dword_126EFB0 != 0);
else
    byte_10657F4 = 1;
```

### C++17 noexcept-in-Type-System (dword_126E270)

A key version-gated feature for CUDA is `dword_126E270`, the C++17 "noexcept is part of the type system" flag. This global is set when `dword_126EF68 >= 201703` and controls whether the lambda preamble injection (step 11 in `sub_6BCC20`) emits `noexcept` specializations of `__nv_hdl_helper_trait_outer`:

```c
// sub_6BCC20, step 11:
if (dword_126E270) {                   // C++17 noexcept in type system
    // Emit 2 additional trait specializations with NeverThrows=true
    // for noexcept-qualified function types
    emit_noexcept_trait_specialization(emit, /* const */ 0);
    emit_noexcept_trait_specialization(emit, /* non-const */ 1);
}
// Closing }; of __nv_hdl_helper_trait_outer emitted unconditionally after
```

Without these specializations, C++17 code using `noexcept` lambdas in host-device contexts would fail to match the wrapper traits, producing template deduction failures.

### Version Interactions with CUDA

The C++ standard version interacts with CUDA semantics in several ways:

- **C++11 minimum**: Most CUDA lambda features require `>= 201103`. Extended lambdas are only meaningful with C++11 lambda syntax.
- **C++14 generic lambdas**: Generic `__device__` lambdas (with `auto` parameters) are gated on `>= 201402`.
- **C++17 structured bindings and if constexpr**: The extended lambda system interacts with `if constexpr` through restriction errors 3620/3621 (constexpr/consteval conflict in lambda operator()).
- **C++20 concepts**: The template variant of cross-space checking (`sub_505B40`) has a concept-context guard that checks `dword_126C5C4` (nested class scope), which is only meaningful with C++20 concepts.

## `--default-device`

This flag is specific to JIT (device-only) compilation mode and changes the default execution space for unannotated entities from `__host__` to `__device__`.

### Mechanism

When enabled, the execution-space assignment logic modifies `entity+182` to receive the `__device__` OR mask (`0x23`) instead of the implicit host default (`0x00`). For variables, `entity+148` bit 0 (`__device__` memory space) is set.

### JIT Mode Context

JIT mode activates when `--gen_c_file_name` (flag 45) is NOT provided -- there is no host output path, so the host backend never runs. This is the compilation mode used by NVRTC (the CUDA runtime compilation library) and the CUDA Driver API's runtime compilation facilities (`cuModuleLoadData`, `cuLinkAddData`).

Without `--default-device`, five JIT-specific diagnostics warn about unannotated entities:

| Diagnostic Tag | Message Summary |
|---|---|
| `no_host_in_jit` | Explicit `__host__` not allowed in JIT mode (no `--default-device` suggestion) |
| `unannotated_function_in_jit` | Unannotated function considered host, not allowed in JIT |
| `unannotated_variable_in_jit` | Namespace-scope variable without memory space annotation |
| `unannotated_static_data_member_in_jit` | Non-const static data member considered host |
| `host_closure_class_in_jit` | Lambda closure class inferred as `__host__` |

Four of the five messages explicitly suggest `--default-device` as a workaround. The exception is `no_host_in_jit` -- an explicit `__host__` annotation cannot be overridden by a flag and requires a source code change.

The `--default-device` flag interacts with the extended lambda system (`dword_106BF38`): when both are active, namespace-scope lambda closure classes infer `__device__` execution space instead of `__host__`, avoiding the `host_closure_class_in_jit` diagnostic.

See [cuda/jit-mode.md](../cuda/jit-mode.md) for full JIT mode documentation.

## `--no-device-int128` / `--no-device-float128`

These two flags (IDs 52 and 53) disable 128-bit integer and floating-point types in device code respectively.

### Registration

Both are registered in `sub_452010` as no-argument mode flags in the CUDA-specific range:

| Flag | ID | Binary Address | Global Effect |
|---|---|---|---|
| `no-device-int128` | 52 | `0x836133` | Disables `__int128` type in device compilation |
| `no-device-float128` | 53 | `0x836144` | Disables `__float128`/`_Float128` in device compilation |

### Purpose

The EDG frontend supports `__int128` (keyword ID 239 in the builtin keyword table) and `_Float128` (keyword ID 335) as extended types. In device code, these types may not be supported by all GPU architectures or may have different semantics than on the host.

The flags belong to the grouped CUDA boolean flags (cases 47--53 in `proc_command_line`), alongside `host-stub-linkage-explicit`, `static-host-stub`, `device-hidden-visibility`, `no-hidden-visibility-on-unnamed-ns`, and `no-multiline-debug`.

Type feature tracking uses `byte_12C7AFC` as a usage flags byte: bit 0 tracks specific integer subtypes (kinds 11, 12), bit 2 tracks `float128`/`bfloat16` usage. The `dword_106C070` global serves as the float128 feature flag, and `dword_106C06C` controls bfloat16.

NVRTC has specific support strings for both types in the binary (`int128 NVRTC`, `float128 NVRTC`), confirming that the JIT compilation path handles the presence or absence of these types explicitly.

## Interaction Matrix

The experimental flags interact with each other and with version gating:

| Interaction | Behavior |
|---|---|
| `--extended-lambda` + C++17 | Enables `noexcept` wrapper trait specializations (step 11 in preamble) via `dword_126E270` |
| `--extended-lambda` + `--expt-relaxed-constexpr` | A `__device__` lambda without the extended-lambda bit is treated as extended if `dword_106BF40` is set (via `sub_6BC680`) |
| `--extended-lambda` + JIT mode | Lambda closure class execution space inference changes; `--default-device` affects namespace-scope lambda inference |
| `--expt-relaxed-constexpr` + cross-space checking | Suppresses 6 specific constexpr cross-space errors; does NOT suppress the 6 non-constexpr variants |
| `--no-device-int128` + NVRTC | NVRTC-specific handling confirms both flags are respected in JIT compilation |
| C++20 + cross-space checking | Concept context guard in `sub_505B40` adds an additional bypass condition for template cross-space calls |

## Global Variable Reference

| Address | Size | Semantic Name | Set By | Checked By |
|---|---|---|---|---|
| `dword_106BF38` | 4 | `extended_lambda_mode` | Flag 79\* (`--extended-lambda`) | `sub_4864F0` (trigger), `sub_489000` (macros), `sub_447930` (scan_lambda) |
| `dword_106BF40` | 4 | `relaxed_constexpr_mode` | Flag 104 (`--relaxed_constexpr`) | `sub_505720` (cross-space call), `sub_6BC680` (device lambda test), `sub_72A650`/`sub_72B510` (symbol ref) |
| `dword_126EF68` | 4 | `cpp_standard_version` | CLI std selection | 28+ functions across all subsystems |
| `dword_126E270` | 4 | `cpp17_noexcept_type` | Post-parsing dialect resolution | `sub_6BCC20` (preamble step 11) |

## Function Reference

| Address | Lines | Identity | Source | Role |
|---|---|---|---|---|
| `sub_452010` | 3,849 | `init_command_line_flags` | `cmd_line.c` | Registers all 276 flags including experimental |
| `sub_459630` | 4,105 | `proc_command_line` | `cmd_line.c` | Parses flags, sets globals |
| `sub_447930` | 2,113 | `scan_lambda` | `cmd_line.c` | Full lambda validation (uses `dword_106BF38`) |
| `sub_4864F0` | 751 | `gen_type_decl` | `cp_gen_be.c` | Preamble injection trigger (checks `dword_106BF38`) |
| `sub_6BCC20` | 244 | `nv_emit_lambda_preamble` | `nv_transforms.c` | Master preamble emitter (17 steps) |
| `sub_505720` | 147 | `check_cross_execution_space_call` | `expr.c` | Cross-space call checker (uses `dword_106BF40`) |
| `sub_505B40` | 92 | `check_cross_space_call_in_template` | `expr.c` | Template variant of cross-space checker |
| `sub_6BC680` | 16 | `is_device_or_extended_device_lambda` | `nv_transforms.c` | Device lambda test (uses `dword_106BF40`) |
| `sub_489000` | 723 | `process_file_scope_entities` | `cp_gen_be.c` | Backend entry; emits false macros when flag off |
| `sub_46E640` | ~400 | `nv_gen_extended_lambda_capture_types` | `cp_gen_be.c` | Capture type declarations for extended lambdas |
| `sub_6BCBF0` | 13 | `nv_record_capture_count` | `nv_transforms.c` | Bitmap bit-set for capture counts |
| `sub_6BCBC0` | ~10 | `nv_reset_capture_bitmaps` | `nv_transforms.c` | Reset both 1024-bit bitmaps |

## Cross-References

- [config/cli-flags.md](cli-flags.md) -- complete flag catalog and registration protocol
- [lambda/overview.md](../lambda/overview.md) -- extended lambda pipeline architecture
- [lambda/preamble-injection.md](../lambda/preamble-injection.md) -- 17-step preamble emission detail
- [lambda/restrictions.md](../lambda/restrictions.md) -- all 35+ lambda restriction error codes
- [cuda/cross-space-validation.md](../cuda/cross-space-validation.md) -- cross-space call checking and `dword_106BF40` relaxation
- [cuda/jit-mode.md](../cuda/jit-mode.md) -- JIT mode, `--default-device` flag, and NVRTC
- [diagnostics/cuda-errors.md](../diagnostics/cuda-errors.md) -- complete CUDA error catalog
