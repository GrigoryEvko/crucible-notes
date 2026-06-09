# Architecture Feature Gating

cudafe++ enforces architecture-dependent feature gates that prevent use of CUDA constructs on hardware that cannot support them. These gates operate at three distinct layers: compile-time SM version checks against `dword_126E4A8` during semantic analysis, string-embedded diagnostic messages with architecture names baked into `.rodata`, and host-compiler version gating controlling which GCC/Clang-specific `#pragma` directives and language constructs appear in the generated `.int.c` output. A separate mechanism, the `--db` debug system, provides runtime tracing that can expose architecture checks as they execute. This page documents all three layers, the global variables involved, every discovered threshold constant, and the complete data flow from nvcc invocation to feature gate evaluation.

## Key Facts

| Property | Value |
|---|---|
| SM version storage | `dword_126E4A8` (`sm_architecture`, set by `--target` / case 245) |
| SM version TU-level copy | `dword_126EBF8` (`target_config_index`, copied during TU init in `sub_586240`) |
| Architecture parser stub | `sub_7525E0` (6-byte stub returning `-1`; actual parsing done by nvcc) |
| Post-parse initializer | `sub_7525F0` (`set_target_configuration`, `target.c:299`) |
| Type table initializer | `sub_7515D0` (sets 100+ type-size/alignment globals, called from `sub_7525F0`) |
| GCC version global | `qword_126EF98` (default `80100` = GCC 8.1.0, set by `--gnu_version` case 184) |
| Clang version global | `qword_126EF90` (default `90100` = Clang 9.1.0, set by `--clang_version` case 188) |
| GCC host dialect flag | `dword_126E1F8` (host compiler identified as GCC) |
| Clang host dialect flag | `dword_126E1E8` (host compiler identified as Clang) |
| Host GCC version copy | `qword_126E1F0` (copied from `qword_126EF98` during dialect init) |
| Host Clang version copy | `qword_126E1E0` (copied from `qword_126EF90` during dialect init) |
| `--nv_arch` error string | `"invalid or no value specified with --nv_arch flag"` at `0x8884F0` |
| Debug option parser | `sub_48A390` (`proc_debug_option`, 238 lines, `debug.c`) |
| Debug trace linked list | `qword_1065870` (head pointer) |
| Invalid arch sentinel | `-1` (`0xFFFFFFFF`) |
| Feature threshold count | 17 CUDA features across 7 SM versions (20, 30, 52, 60, 70, 80, 90/90a) |
| Host compiler threshold count | 19 version constants across GCC 3.0 through GCC 14.0 |

## Layer 1: SM Architecture Input

### How the Architecture Reaches cudafe++

cudafe++ never parses architecture strings directly from the user. The driver (`nvcc`) translates user-facing flags like `--gpu-architecture=sm_90` into an internal numeric code and passes it via the `--target` flag when spawning the cudafe++ process. Inside cudafe++, the `--target` flag is registered as CLI flag 245 and handled in `proc_command_line` (`sub_459630`).

The handler calls `sub_7525E0`, which in the CUDA Toolkit 13.0 binary is a 6-byte stub:

```asm
; sub_7525E0 -- architecture parser stub
; Address: 0x7525E0, Size: 6 bytes
mov     eax, 0FFFFFFFFh    ; return -1 unconditionally
retn
```

This stub always returns `-1` (the invalid-architecture sentinel). The actual architecture code is injected by nvcc into the argument string that `sub_7525E0` receives. Because IDA decompiled this as a stub, the parsing logic is either inlined by the compiler or resolved through a different mechanism at link time. The result is stored in `dword_126E4A8`:

```c
// proc_command_line (sub_459630), case 245
v80 = sub_7525E0(qword_E7FF28, v23, v20, v30);  // parse SM code from arg string
dword_126E4A8 = v80;                              // store in sm_architecture
if (v80 == -1) {
    sub_4F8420(2664);  // emit error 2664
    // error string: "invalid or no value specified with --nv_arch flag"
    sub_4F2930("cmd_line.c", 12219, "proc_command_line", 0, 0);
    // assert_fail -- unreachable if error handler returns
}
sub_7525F0(v80);  // set_target_configuration
```

Error 2664 fires when the architecture value is `-1`. The error string at `0x8884F0` references `--nv_arch` (the nvcc-facing name for this flag). This string has no direct xrefs in the IDA analysis, meaning it is loaded indirectly through the error message table (`off_88FAA0`). The `--nv_arch` name in the error message is a user-facing alias; internally cudafe++ processes it as `--target` (flag 245).

### set_target_configuration (sub_7525F0)

After storing the SM version, `sub_7525F0` performs post-parse initialization. This function lives in `target.c:299`:

```c
// sub_7525F0 -- set_target_configuration
__int64 __fastcall sub_7525F0(int a1)
{
    if ((unsigned int)(a1 + 1) > 1)  // rejects only -1
        assert_fail("set_target_configuration", 299);
    sub_7515D0();           // initialize type table for target platform
    qword_126E1B0 = "lib";  // library search path prefix
}
```

The guard `(a1 + 1) > 1u` is an unsigned comparison that accepts any value >= 0 and rejects only `-1` (which wraps to 0 when incremented). This is a sanity check — in production, nvcc always provides a valid SM code.

### Type Table Initialization (sub_7515D0)

The `sub_7515D0` function, called from `set_target_configuration`, initializes over 100 global variables describing the target platform's type sizes, alignments, and numeric limits. This establishes the data model for CUDA device code:

```c
// sub_7515D0 -- target type initialization (excerpt)
// Sets LP64 data model with CUDA-specific type properties
dword_126E328 = 8;     // sizeof(long)
dword_126E338 = 4;     // sizeof(int)
dword_126E2FC = 16;    // sizeof(long double)
dword_126E308 = 16;    // alignof(long double)
dword_126E2B8 = 8;     // sizeof(pointer)
dword_126E2AC = 8;     // alignof(pointer)
dword_126E420 = 2;     // sizeof(wchar_t)
dword_126E4A0 = 8;     // target vector width
dword_126E258 = 53;    // double mantissa bits
dword_126E250 = 1024;  // double max exponent
dword_126E254 = -1021; // double min exponent
dword_126E234 = 113;   // __float128 mantissa bits
dword_126E22C = 0x4000; // __float128 max exponent
dword_126E230 = -16381; // __float128 min exponent
// ... ~80 more assignments ...
```

The function unconditionally returns `-1`, which is not used by the caller.

### SM Version Propagation

During translation unit initialization (`sub_586240`, called from `fe_translation_unit_init`), the SM version is copied into a TU-level global:

```c
// sub_586240, line 54 in decompiled output
dword_126EBF8 = dword_126E4A8;  // target_config_index = sm_architecture
```

After this point, architecture checks throughout the compiler read either `dword_126E4A8` (the CLI-level global) or `dword_126EBF8` (the TU-level copy). Both contain the same integer SM version code. The dual-variable pattern exists because EDG's architecture supports multi-TU compilation where each TU could theoretically target a different architecture (though CUDA compilation always uses a single target per cudafe++ invocation).

## Layer 2: CUDA Feature Thresholds

cudafe++ checks the SM architecture version at semantic analysis time to gate CUDA-specific features. When a feature is used on an architecture below its minimum requirement, the compiler emits a diagnostic error or warning. All thresholds below were extracted from error strings embedded in the binary's `.rodata` section and confirmed through cross-reference with diagnostic tag names.

### Complete Feature Threshold Table

| Feature | Min Architecture | Diagnostic Tag | Error String |
|---|---|---|---|
| Virtual base classes | compute_20 | `use_of_virtual_base_on_compute_1x` | `Use of a virtual base (%t) requires the compute_20 or higher architecture` |
| Device variadic functions | compute_30 | `device_function_has_ellipsis` | `__device__ or __host__ __device__ function with ellipsis requires compute_30 or higher architecture` |
| `__managed__` variables | compute_30 | `unsupported_arch_for_managed_capability` | `__managed__ variables require architecture compute_30 or higher` |
| `alloca()` in device code | compute_52 | `alloca_unsupported_for_lower_than_arch52` | `alloca() is not supported for architectures lower than compute_52` |
| Atomic scope argument | sm_60 | *(inline)* | `atomic operations' scope argument is supported on architecture sm_60 or above. Fall back to use membar.` |
| Atomic f64 add/sub | sm_60 | *(inline)* | `atomic add and sub for 64-bit float is supported on architecture sm_60 or above.` |
| `__nv_atomic_*` functions | sm_60 | *(inline)* | `__nv_atomic_* functions are not supported on arch < sm_60.` |
| `__grid_constant__` | compute_70 | `grid_constant_unsupported_arch` | `__grid_constant__ annotation is only allowed for architecture compute_70 or later` |
| Atomic memory order | sm_70 | *(inline)* | `atomic operations' argument of memory order is supported on architecture sm_70 or above. Fall back to use membar.` |
| 128-bit atomic load/store | sm_70 | *(inline)* | `128-bit atomic load and store are supported on architecture sm_70 or above.` |
| 16-bit atomic CAS | sm_70 | *(inline)* | `16-bit atomic compare-and-exchange is supported on architecture sm_70 or above.` |
| `__nv_register_params__` | compute_80 | `register_params_unsupported_arch` | `__nv_register_params__ is only supported for compute_80 or later architecture` |
| `__wgmma_mma_async` | sm_90a | `wgmma_mma_async_not_enabled` | `__wgmma_mma_async builtins are only available for sm_90a` |
| Atomic cluster scope | sm_90 | *(inline)* | `atomic operations' scope of cluster is supported on architecture sm_90 or above. Using device scope instead.` |
| Atomic cluster scope (load/store) | sm_90 | *(inline)* | `atomic load and store's scope of cluster is supported on architecture sm_90 or above. Using device scope instead.` |
| 128-bit atomic exch/CAS | sm_90 | `nv_atomic_exch_cas_b128_not_supported` | `128-bit atomic exchange or compare-and-exchange is supported on architecture sm_90 or above.` |

### GPU-Architecture-Gated Attributes (No Specific SM in String)

Several features check the architecture but their error strings do not embed a specific SM version number. Instead, they use the generic phrase "this GPU architecture", meaning the threshold is encoded in the comparison logic rather than the diagnostic text:

| Feature | Diagnostic Tag | Error String |
|---|---|---|
| `__cluster_dims__` | `cluster_dims_unsupported` | `__cluster_dims__ is not supported for this GPU architecture` |
| `max_blocks_per_cluster` | `max_blocks_per_cluster_unsupported` | `cannot specify max blocks per cluster for this GPU architecture` |
| `__block_size__` | `block_size_unsupported` | `__block_size__ is not supported for this GPU architecture` |
| `__managed__` (config) | `unsupported_configuration_for_managed_capability` | `__managed__ variables are not yet supported for this configuration (compilation mode (32/64 bit) and/or target operating system)` |

These features are gated by the same `dword_126E4A8` comparison mechanism as the features in the main table, but their exact SM threshold values would require tracing the specific comparison instructions in the semantic analysis functions.

### Diagnostic Behavior: Errors vs Warnings vs Demotions

Architecture gate violations produce three distinct behaviors depending on the feature class:

**Hard errors** — Compilation halts. Features that fundamentally cannot work on the target architecture:
- `__managed__` below compute_30 — No unified memory hardware support
- `__grid_constant__` below compute_70 — No hardware constant propagation mechanism
- `__nv_register_params__` below compute_80 — Register parameter ABI not available
- `__wgmma_mma_async` below sm_90a — No warp-group MMA hardware
- `alloca()` below compute_52 — No dynamic stack allocation support on device
- Virtual base classes below compute_20 — No vtable support on earliest GPU architectures

**Fallback warnings** — Compilation continues with degraded behavior. The compiler generates functionally correct but potentially less performant code:
- Atomic scope arguments on pre-sm_60 — Falls back to `membar`-based synchronization
- Atomic memory order on pre-sm_70 — Falls back to `membar`-based ordering
- 64-bit float atomics on pre-sm_60 — Falls back to CAS loop emulation

**Scope demotion warnings** — Informational diagnostics about automatic scope narrowing:
- Cluster scope atomics on pre-sm_90 — Silently demotes to device scope ("Using device scope instead")

### compute_XX vs sm_XX Naming

Error strings use two naming conventions that reflect CUDA's split between virtual and physical architectures:

- **`compute_XX`** — Virtual architecture. Checked at PTX generation time. Features gated by `compute_XX` are relevant to the intermediate PTX representation and are independent of the specific GPU die. Examples: `__managed__` (requires unified memory ISA support), `alloca()` (requires dynamic stack frame instructions).

- **`sm_XX`** — Physical architecture. Checked at SASS generation time. Features gated by `sm_XX` are tied to specific hardware capabilities of a GPU die. Examples: 128-bit atomics (require specific load/store unit widths), cluster scope (requires the SM 9.0 thread block cluster hardware).

In practice, cudafe++ stores a single integer in `dword_126E4A8` and the distinction is purely semantic — both forms gate against the same numeric value. The value is a compute capability number (e.g., 70 for Volta, 90 for Hopper).

The **`sm_90a`** suffix (with the `a` accelerator flag) is a special case used exclusively for `__wgmma_mma_async` builtins. This variant requires the Hopper accelerated architecture, which is distinct from the base sm_90. The `a` suffix is encoded in the SM integer value passed to cudafe++ by nvcc.

### __wgmma_mma_async Detail

The warp-group matrix multiply-accumulate builtin has the most granular validation of any architecture-gated feature. Beyond the sm_90a architecture check, cudafe++ also validates:

| Check | Diagnostic Tag | Error String |
|---|---|---|
| Architecture gate | `wgmma_mma_async_not_enabled` | `__wgmma_mma_async builtins are only available for sm_90a` |
| Shape validation | `wgmma_mma_async_bad_shape` | `The shape %s is not supported for __wgmma_mma_async builtin` |
| A operand type | `wgmma_mma_async_bad_A_type` | *(type mismatch diagnostic)* |
| B operand type | `wgmma_mma_async_bad_B_type` | *(type mismatch diagnostic)* |
| Missing arguments | `wgmma_mma_async_missing_args` | `The 'A' or 'B' argument to __wgmma_mma_async call is missing` |
| Non-constant args | `wgmma_mma_async_nonconstant_arg` | `Non-constant argument to __wgmma_mma_async call` |

The validation function is identified as `check_wgmma_mma_async` (string at `0x888CAC`). Four type-specific builtin variants are registered: `__wgmma_mma_async_f16`, `__wgmma_mma_async_bf16`, `__wgmma_mma_async_tf32`, and `__wgmma_mma_async_f8`.

### __nv_register_params__ Detail

The register parameter attribute has three distinct checks, only one of which is an architecture gate:

| Check | Diagnostic Tag | Error String |
|---|---|---|
| Feature enable flag | `register_params_not_enabled` | `__nv_register_params__ support is not enabled` |
| Architecture gate | `register_params_unsupported_arch` | `__nv_register_params__ is only supported for compute_80 or later architecture` |
| Function type check | `register_params_unsupported_function` | `__nv_register_params__ is not allowed on a %s function` |
| Ellipsis check | `register_params_ellipsis_function` | *(variadic function diagnostic)* |

The attribute handler is `apply_nv_register_params_attr` (string at `0x830C78`).

### SM Version to Feature Summary

| SM Version | Features Introduced | Feature Count |
|---|---|---|
| compute_20 | Virtual base classes in device code | 1 |
| compute_30 | `__managed__` variables, device variadic functions | 2 |
| compute_52 | `alloca()` in device code | 1 |
| sm_60 | Atomic scope argument, 64-bit float atomics, `__nv_atomic_*` API | 3 |
| sm_70 | `__grid_constant__`, 128-bit atomic load/store, atomic memory order, 16-bit CAS | 4 |
| compute_80 | `__nv_register_params__` | 1 |
| sm_90 / sm_90a | `__wgmma_mma_async`, thread block clusters, 128-bit atomic exchange/CAS, cluster scope atomics | 5 |

Notably absent from cudafe++ error strings are features like cooperative groups (sm_60+), tensor cores (sm_70+), and dynamic parallelism (sm_35+). These are checked at runtime or by the PTX assembler (`ptxas`) rather than the language frontend.

## Layer 3: Host Compiler Version Gating

cudafe++ generates `.int.c` output that must compile cleanly under the host C++ compiler (GCC, Clang, or MSVC). Because different host compiler versions support different warning pragmas, attributes, and language features, cudafe++ gates its output based on the host compiler version stored in `qword_126EF98` (GCC) and `qword_126EF90` (Clang). Additionally, several C++ language feature flags in the EDG frontend are conditionally enabled based on host compiler version to match the behavior the user expects from their host compiler.

### Version Encoding

Both GCC and Clang versions are encoded as a single integer: `major * 10000 + minor * 100 + patch`. For example, GCC 8.1.0 is encoded as `80100`. The compiler tests these values against hexadecimal threshold constants using `>` (strictly-greater-than) comparisons, which effectively means "version at or above threshold + 1." Since all threshold values use a `99` patch level (e.g., `40299` for GCC 4.2.99), the gate `> 40299` is equivalent to `>= 40300`, which effectively means "GCC 4.3 or later."

### Complete Threshold Table

| Hex Constant | Decimal | Encoded Version | Effective Gate | Occurrence Count |
|---|---|---|---|---|
| `0x752F` | 29,999 | 2.99.99 | GCC/Clang >= 3.0 | 1 (dialect resolution) |
| `0x75F7` | 30,199 | 3.01.99 | GCC/Clang >= 3.2 | low |
| `0x76BF` | 30,399 | 3.03.99 | GCC/Clang >= 3.4 | low (cuda_compat_flag gate) |
| `0x7787` | 30,599 | 3.05.99 | Clang >= 3.6 | medium (`-Wunused-local-typedefs`) |
| `0x78B3` | 30,899 | 3.08.99 | Clang >= 3.9 | low |
| `0x9C3F` | 39,999 | 3.99.99 | GCC >= 4.0 | medium (dword_106BDD8 + Clang gate) |
| `0x9D07` | 40,199 | 4.01.99 | GCC >= 4.2 | medium (`-Wunused-variable` file-level) |
| `0x9D6B` | 40,299 | 4.02.99 | GCC >= 4.3 | medium (variadic templates) |
| `0x9DCF` | 40,399 | 4.03.99 | GCC >= 4.4 | low (dialect resolution) |
| `0x9E33` | 40,499 | 4.04.99 | GCC >= 4.5 | low (dialect resolution) |
| `0x9E97` | 40,599 | 4.05.99 | GCC >= 4.6 | medium (diagnostic push/pop) |
| `0x9EFB` | 40,699 | 4.06.99 | GCC >= 4.7 | low (feature flag gating) |
| `0x9F5F` | 40,799 | 4.07.99 | GCC >= 4.8 | medium (`-Wunused-local-typedefs`) |
| `0xEA5F` | 59,999 | 5.99.99 | GCC >= 6.0 | **22 files** (C++14/17 features) |
| `0xEB27` | 60,199 | 6.01.99 | GCC >= 6.2 | low (`HasFuncPtrConv` gate) |
| `0x1116F` | 69,999 | 6.99.99 | GCC >= 7.0 | medium (dword_106BDD8 + feature flags) |
| `0x15F8F` | 89,999 | 8.99.99 | GCC/Clang >= 9.0 | medium (C++17/20 features) |
| `0x1D4BF` | 119,999 | 11.99.99 | GCC/Clang >= 12.0 | 8 files |
| `0x1FBCF` | 129,999 | 12.99.99 | GCC >= 13.0 | 13 files |
| `0x222DF` | 139,999 | 13.99.99 | GCC >= 14.0 | 5 files |

### How Thresholds Are Used

The thresholds serve three purposes:

**1. Diagnostic pragma emission.** The `.int.c` output includes `#pragma GCC diagnostic` directives to suppress host compiler warnings about CUDA-generated code. Different GCC/Clang versions introduced different warning flags, so the pragmas are conditionally emitted:

```c
// From sub_489000 (backend boilerplate emission)
// -Wunused-local-typedefs: GCC 4.8+ (0x9F5F) or Clang 3.6+ (0x7787)
if ((dword_126E1E8 && qword_126EF90 > 0x7787)
    || (!dword_106BF6C && !dword_106BF68
        && dword_126E1F8 && qword_126E1F0 > 0x9F5F))
{
    emit("#pragma GCC diagnostic ignored \"-Wunused-local-typedefs\"");
}

// Push/pop block for managed RT: GCC 4.6+ (0x9E97) or Clang
if (dword_126E1E8 || (!dword_106BF6C && dword_126E1F8 && qword_126E1F0 > 0x9E97))
{
    emit("#pragma GCC diagnostic push");
    emit("#pragma GCC diagnostic ignored \"-Wunused-variable\"");
    emit("#pragma GCC diagnostic ignored \"-Wunused-function\"");
    // ... managed runtime boilerplate ...
    emit("#pragma GCC diagnostic pop");
}

// File-level -Wunused-variable: GCC 4.2+ (0x9D07) or Clang
if (dword_126E1E8 || (dword_126E1F8 && qword_126E1F0 > 0x9D07))
    emit("#pragma GCC diagnostic ignored \"-Wunused-variable\"");
```

**2. C++ feature gating during dialect resolution.** The post-parsing dialect resolution in `proc_command_line` and the `sub_44B6B0` dialect setup function use `qword_126EF98` thresholds to decide which C++ language features to enable. Examples from the decompiled code:

```c
// sub_44B6B0 -- dialect resolution, ~400 lines
// GCC 4.3+ (0x9D6B): enable variadic templates
if (qword_126EF98 > 0x9D6B)
    dword_106BE1C = 1;  // variadic_templates

// GCC 4.7+ (0x9EFB): enable list initialization under certain conditions
if (qword_126EF98 > 0x9EFB && dword_106BE1C && (!byte_E7FFF1 || dword_106C10C))
    dword_106BE10 = 1;

// GCC 6.0+ (0xEA5F) or Clang: enable C++14/17 features
if (dword_126EFA4 || (dword_126EFA8 && qword_126EF98 > 0xEA5F))
    // Enable feature (Clang always, GCC only 6.0+)
```

**3. CUDA compatibility mode.** A special flag `dword_E7FF10` (`cuda_compat_flag`) is set when `dword_126EFAC && qword_126EF98 <= 0x76BF` — that is, when extended features are enabled but the GCC version is 3.3.99 or below. This activates a legacy compatibility path for very old host compilers that lack modern C++ support.

### The 0xEA5F (59999) Threshold — The Most Pervasive Gate

The threshold `0xEA5F` (GCC 6.0) is the most widely used version constant in the binary, appearing in **22 decompiled functions**. It gates the C++14/17 feature set boundary. GCC 6.0 was the first GCC release with full C++14 support and substantial C++17 support.

The typical usage pattern is:

```c
// Pattern: "Clang (any version) OR GCC 6.0+"
if (dword_126EFA4 || (dword_126EFA8 && qword_126EF98 > 0xEA5F))
    // Enable C++14/17 feature

// Pattern: "GNU extensions but not Clang, GCC 6.0+"
if (dword_126EFAC && !dword_126EFA4 && qword_126EF98 > 0xEA5F)
    // Enable GNU-specific extended feature
```

Functions using this threshold include: declaration processing (`sub_40D900`), attribute application (`sub_413ED0`), class declaration (`sub_431590`), dialect resolution (`sub_44B6B0`), initializer processing (`sub_48C710`, `sub_4B6760`), backend code generation (`sub_4688C0`), expression canonicalization (`sub_4CA6C0`, `sub_4D2B70`), IL walking (`sub_54AED0`), scope management (`sub_59C9B0`, `sub_59AF40`), type processing (`sub_5D1350`), overload resolution (`sub_662670`, `sub_666720`), and template specialization (`sub_6A3B00`).

### Version-Gated Feature Flag: dword_106BDD8

One particular feature flag (`dword_106BDD8`) is set during dialect resolution based on a compound version check:

```c
// sub_44B6B0, decompiled line ~228-231
// v4 = (dword_126EFA4 != 0), i.e., is_clang_mode
if ((dword_126EFAC && !v4 && qword_126EF98 > 0x1116F)  // GNU ext, not Clang, GCC >= 7.0
    || (v4 && qword_126EF90 > 0x9C3F))                   // or Clang >= 4.0
{
    dword_106BDD8 = 1;
}
```

This flag is referenced in 7 decompiled functions (`sub_430920`, `sub_42FE50`, `sub_447930`, `sub_44AAC0`, `sub_44B6B0`, `sub_45EB40`, `sub_724630`). The W066 global variables report identifies it as `optix_mode`, but the decompiled code shows it is set purely based on compiler version thresholds during dialect resolution, not from any `--emit-optix-ir` CLI flag. It likely controls a C++ language feature (possibly structured bindings or another C++17 feature) that requires GCC 7.0+ or Clang 4.0+ support, and the "optix_mode" name in the report may be a misidentification based on context where it was encountered. The flag gates behavior in attribute validation (`sub_42FE50`), where it interacts with `dword_106B670` to control feature availability.

### Dialect Initialization Flow

The host compiler version globals are initialized in `proc_command_line` and propagated to the dialect system during TU initialization:

```text
proc_command_line (CLI parsing, sub_459630):
  case 184 (--gnu_version=X):   qword_126EF98 = X   // GCC version
  case 188 (--clang_version=X): qword_126EF90 = X   // Clang version
  case 182 (--gcc):             dword_126EFA8 = 1    // GCC mode flag
  case 187 (--clang):           dword_126EFA4 = 1    // Clang mode flag

dialect_init (sub_44B6B0, called during setup):
  // ~400 lines of version-threshold-based feature flag resolution
  // Sets 30+ EDG feature flags based on gcc_version, clang_version,
  // cpp_standard_version, and extension mode flags

target dialect (sub_752A80, select_cp_gen_be_target_dialect):
  if (dword_126EFA8):                           // GCC mode
    dword_126E1F8 = 1                           // host_dialect_gnu
    qword_126E1F0 = qword_126EF98              // host_gcc_version
  if (dword_126EFA4):                           // Clang mode
    dword_126E1E8 = 1                           // host_dialect_clang
    qword_126E1E0 = qword_126EF90              // host_clang_version
```

The defaults for unspecified versions are `qword_126EF98 = 80100` (GCC 8.1.0) and `qword_126EF90 = 90100` (Clang 9.1.0), set during `default_init` (`sub_45EB40`).

## The --db Debug Mechanism

The `--db` flag (CLI case 37) activates EDG's internal debug tracing system by calling `sub_48A390` (`proc_debug_option`). While not directly related to architecture gating, the `--db` mechanism shares the adjacent global namespace (`dword_126EFC8`, `dword_126EFCC`) and is relevant because debug tracing can expose architecture checks as they execute in real time.

### Connection Between --db and Architecture

The `--db` flag does not set or modify any architecture-related globals. Its connection to the architecture system is observational: when debug tracing is enabled, the compiler emits trace output at key decision points throughout compilation, including the semantic analysis functions that evaluate architecture thresholds. Enabling `--db=5` (verbosity level 5) causes the compiler to log IL entry kinds, template instantiation steps, and scope transitions, which provides visibility into when and why architecture gates fire.

The CLI dispatch for `--db`:

```c
// proc_command_line (sub_459630), case 37
case 37:  // --db=<string>
    if (sub_48A390(qword_E7FF28))  // proc_debug_option
        goto error;                // returns nonzero on parse failure
    dword_106C2A0 = dword_126EFCC; // save initial error count baseline
```

After `proc_debug_option` returns, `dword_106C2A0` captures the current value of `dword_126EFCC` (debug verbosity level). This is used as a baseline error count for subsequent error tracking.

### proc_debug_option (sub_48A390)

This 238-line function (`debug.c`) parses debug control strings. On entry, it unconditionally sets `dword_126EFC8 = 1` (debug tracing enabled), then dispatches based on the first character of the input:

```c
// sub_48A390 entry
dword_126EFC8 = 1;  // enable debug tracing
v3 = (unsigned __int8)*nptr;
if ((v3 - 48) <= 9) {               // first char is digit
    dword_126EFCC = strtol(v1, 0, 10); // set verbosity level
    return 0;
}
```

The full parsing grammar:

| Input Format | Parsed As | Action |
|---|---|---|
| `"5"` (numeric only) | Verbosity level | Sets `dword_126EFCC = 5` |
| `"name=3"` | Name with level | Adds trace node: action=1, level=3 |
| `"name+=3"` | Additive trace | Adds trace node: action=2, level=3 |
| `"name-=3"` | Subtractive trace | Adds trace node: action=3, level=3 |
| `"name=3!"` | Permanent trace | Adds trace node: action=1, level=3, permanent=1 |
| `"#name"` | Hash removal | Removes matching node from trace list |
| `"-name"` | Dash removal | Removes matching node from trace list |
| `"a,b=2,c=3"` | Comma-separated | Processes each entry independently |

### Debug Trace Node Structure

Debug trace requests are stored as a singly-linked list rooted at `qword_1065870`. Each node is 28 bytes, allocated via `sub_6B7340` (the IL allocator):

```c
struct debug_trace_node {           // 28 bytes (32 allocated)
    struct debug_trace_node* next;  // +0:  linked list link
    char*  name_string;             // +8:  entity name to trace (heap copy)
    int32  action_type;             // +16: 1=set, 2=add, 3=subtract, 4=remove
    int32  level;                   // +20: trace level (integer)
    int32  permanent;               // +24: 1=survives reset, 0=cleared on reset
};
```

When `proc_debug_option` encounters its own name in the trace list (the self-referential check `!strcmp(src, "proc_debug_option")`), it prints the entire trace state to stderr:

```c
if (qword_1065870 && (v2 & 1) != 0) {
    do {
        fprintf(s, "debug request for: %s\n", node->name_string);
        fprintf(s, "action=%d,  level=%d\n", node->action_type, node->level);
        node = node->next;
    } while (node);
}
```

### Debug Verbosity Levels

The `dword_126EFCC` verbosity level controls trace output granularity across the entire compiler:

| Level | Effect |
|---|---|
| 0 | No debug output (default) |
| 1-2 | Basic trace: function entry/exit markers |
| 3 | Detailed trace: includes entity names, scope indices |
| 4 | Very detailed: IL entry kinds, overload candidate lists |
| 5+ | Full trace: IL tree walking with `"Walking IL tree, entry kind = ..."` |

### db_name (CLI case 190)

The `--db_name` flag (case 190) calls a separate function `sub_48AD80` to register a debug name filter. Unlike `--db` which enables global tracing, `--db_name` restricts trace output to entities matching the specified name pattern. If `sub_48AD80` fails (returns nonzero), error 570 is emitted.

## Three-Layer Checking Model

### Layer 1: Compile-Time Semantic Checks (cudafe++ Frontend)

These are the primary gates. During semantic analysis, cudafe++ reads `dword_126E4A8` and compares it against threshold constants. Violations emit diagnostic errors through the standard error system (diagnostic IDs in the 3000+ range, displayed as 20000-series via the `+16543` offset formula). These checks are unconditional — they fire regardless of whether the code would actually execute at runtime.

**Enforcement point:** Declaration processing, type checking, attribute application, and CUDA-specific semantic validation passes.

**Examples:**
- `__managed__` variable declaration with `dword_126E4A8 < 30` triggers `unsupported_arch_for_managed_capability`
- `__grid_constant__` parameter with `dword_126E4A8 < 70` triggers `grid_constant_unsupported_arch`
- `__wgmma_mma_async` call on non-sm_90a triggers `wgmma_mma_async_not_enabled`
- Virtual base class with `dword_126E4A8 < 20` triggers `use_of_virtual_base_on_compute_1x`

### Layer 2: String-Embedded Diagnostic Formatting

Error strings with architecture names baked into `.rodata` represent the complete set of architecture-dependent diagnostics. These strings are loaded by the diagnostic system and formatted with the current architecture value. The strings serve as the user-visible feedback for Layer 1 checks.

The architecture name in the string (e.g., `"compute_70"`, `"sm_90a"`) is a literal constant, not a formatted parameter — the compiler does not interpolate the actual target architecture into these messages. This means the error messages always state the **minimum required** architecture, not what the user actually specified. The only exception is the virtual base error which uses `%t` (a type formatter) to include the base class name, not the architecture.

### Layer 3: Host Compiler Version Gating

This layer does not check GPU architecture at all — instead, it gates the **output format** of the generated `.int.c` file based on the host C++ compiler's version. The thresholds ensure that GCC/Clang-specific pragmas, attributes, and language constructs in the generated code are compatible with the actual host compiler that will consume the output.

**Enforcement point:** Backend code generation (`sub_489000` and related functions in `cp_gen_be.c`).

**Impact:** Incorrect host compiler version gating does not cause compilation failure — it may produce warnings from the host compiler due to unrecognized pragmas, or miss warning suppression directives that would silence spurious diagnostics.

### Interaction Between Layers

```text
nvcc (driver)
  |
  | --target=<sm_code>  --gnu_version=<ver>  --clang_version=<ver>
  v
cudafe++ process
  |
  +-- CLI parsing (proc_command_line)
  |     dword_126E4A8 = sm_code         (SM architecture)
  |     qword_126EF98 = gcc_version     (host GCC version)
  |     qword_126EF90 = clang_version   (host Clang version)
  |
  +-- set_target_configuration (sub_7525F0)
  |     sub_7515D0()  -- type table init (100+ globals)
  |
  +-- dialect_resolution (sub_44B6B0)
  |     30+ feature flags set based on version thresholds
  |     dword_126E1F8 / dword_126E1E8  -- host dialect set
  |     qword_126E1F0 / qword_126E1E0  -- host version copies
  |
  +-- TU init (sub_586240)
  |     dword_126EBF8 = dword_126E4A8   (SM version copy)
  |
  +-- [Layer 1] Semantic analysis
  |     Compare dword_126E4A8 against SM thresholds
  |     Emit CUDA-specific errors for unsupported features
  |
  +-- [Layer 2] Diagnostic formatting
  |     Load error string with baked-in architecture name
  |     Format and display error to user
  |
  +-- [Layer 3] .int.c code generation
  |     Compare qword_126E1F0 / qword_126E1E0 against host thresholds
  |     Emit appropriate #pragma directives
  |     Generate host-compiler-compatible boilerplate
  |
  v
Host Compiler (gcc / clang / cl.exe)
```

Layers 1 and 2 operate during the frontend phase and can halt compilation. Layer 3 operates during the backend phase and only affects the format of the generated output file.

## Global Variable Summary

| Address | Size | Name | Role |
|---|---|---|---|
| `dword_126E4A8` | 4 | `sm_architecture` | Target SM version from `--target` (case 245). Sentinel: `-1`. |
| `dword_126EBF8` | 4 | `target_config_index` | TU-level copy of `dword_126E4A8`, set in `sub_586240`. |
| `qword_126EF98` | 8 | `gcc_version` | GCC compatibility version. Default `80100`. Set by `--gnu_version` (case 184). |
| `qword_126EF90` | 8 | `clang_version` | Clang compatibility version. Default `90100`. Set by `--clang_version` (case 188). |
| `dword_126EFA8` | 4 | `gcc_extensions` | GCC mode enabled. Set by `--gcc` (case 182). |
| `dword_126EFA4` | 4 | `clang_extensions` | Clang mode enabled. Set by `--clang` (case 187). |
| `dword_126EFAC` | 4 | `extended_features` | Extended features / GNU compat mode. |
| `dword_126EFB0` | 4 | `gnu_extensions_enabled` | GNU extensions active. |
| `dword_126E1F8` | 4 | `host_dialect_gnu` | Host compiler is GCC/GNU. Set during dialect init. |
| `dword_126E1E8` | 4 | `host_dialect_clang` | Host compiler is Clang. Set during dialect init. |
| `qword_126E1F0` | 8 | `host_gcc_version` | Host GCC version, copied from `qword_126EF98`. |
| `qword_126E1E0` | 8 | `host_clang_version` | Host Clang version, copied from `qword_126EF90`. |
| `dword_126EFC8` | 4 | `debug_trace_enabled` | Debug tracing active. Set unconditionally by `--db`. |
| `dword_126EFCC` | 4 | `debug_verbosity` | Debug output level. >2=detailed, >4=IL walk trace. |
| `dword_E7FF10` | 4 | `cuda_compat_flag` | Legacy compat: `dword_126EFAC && qword_126EF98 <= 0x76BF`. |
| `dword_106BDD8` | 4 | `version_gated_feature` | Set when GCC >= 7.0 or Clang >= 4.0. Referenced in 7 functions. |
| `dword_106C2A0` | 4 | `error_count_baseline` | Saved from `dword_126EFCC` after `--db` processing. |
| `qword_1065870` | 8 | `debug_trace_list` | Head of debug trace request linked list. |
| `dword_126E4A0` | 4 | `target_vector_width` | Set to 8 by `sub_7515D0`. |

## Cross-References

- [CLI Flag Inventory](../config/cli-flags.md) — `--target`, `--gnu_version`, `--clang_version`, `--db` flag details
- [Architecture Detection](../config/arch-detection.md) — `--target` flag and SM version parsing details
- [CUDA Error Catalog](../diagnostics/cuda-errors.md) — Complete diagnostic messages for each feature gate
- [.int.c File Format](../output/int-c-format.md) — Host compiler pragma emission details
- [Backend Code Generation](../pipeline/backend.md) — GCC/Clang version threshold usage in output
- [Global Variable Index](../reference/global-variables.md) — Full address-level documentation
- [Execution Spaces](./execution-spaces.md) — Execution space bitfield and attribute handlers
- [\_\_managed\_\_ Variables](../attributes/managed-variables.md) — Managed variable attribute and SM 30 gate
- [\_\_grid\_constant\_\_](../attributes/grid-constant.md) — Grid constant attribute and SM 70 gate
