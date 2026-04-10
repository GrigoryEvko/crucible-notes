# Architecture Feature Gating

cudafe++ enforces a comprehensive set of architecture-dependent feature gates that prevent use of CUDA constructs on hardware that does not support them. These gates operate at three distinct layers: compile-time checks inside the frontend (error emission during semantic analysis), string-embedded threshold checks (diagnostic messages with architecture names baked into the binary's `.rodata`), and host-compiler version gating (controlling which GCC/Clang-specific `#pragma` and warning suppression directives appear in the generated `.int.c` output). This page documents all three layers, the global variables involved, and the complete set of discovered thresholds.

## Key Facts

| Property | Value |
|---|---|
| SM version storage | `dword_126E4A8` (`sm_architecture`, set by `--target` / case 245) |
| SM version copy | `dword_126EBF8` (`target_config_index`, copied from `dword_126E4A8` during TU init) |
| Architecture parser | `sub_7525E0` (stub returning -1; actual parsing done by nvcc before invocation) |
| Post-parse initializer | `sub_7525F0` (`set_target_configuration`, calls `sub_7515D0` type init) |
| GCC version global | `qword_126EF98` (default `80100` = GCC 8.1.0) |
| Clang version global | `qword_126EF90` (default `90100` = Clang 9.1.0) |
| GCC host flag | `dword_126E1F8` (host dialect set) |
| Clang host flag | `dword_126E1E8` (device/alternative dialect set) |
| Host GCC version | `qword_126E1F0` (copied from `qword_126EF98` during dialect init) |
| Host Clang version | `qword_126E1E0` (copied from `qword_126EF90` during dialect init) |
| `--nv_arch` error string | `"invalid or no value specified with --nv_arch flag"` at `0x8884F0` |
| Debug option parser | `sub_48A390` (`proc_debug_option`, 238 lines, `debug.c`) |
| Invalid arch sentinel | `-1` (0xFFFFFFFF) |

## Layer 1: SM Architecture Input

### How the Architecture Reaches cudafe++

cudafe++ never parses architecture strings directly from the user. The `--target` flag (CLI case 245) calls `sub_7525E0`, which in the CUDA Toolkit 13.0 binary is a 6-byte stub that returns `-1`:

```asm
; sub_7525E0, 6 bytes
mov     eax, 0FFFFFFFFh
retn
```

The actual SM architecture value is injected by nvcc, which translates user-facing flags like `--gpu-architecture=sm_90` into internal numeric codes before spawning the cudafe++ process. The `--target` handler stores the parsed value into `dword_126E4A8`:

```c
// proc_command_line, case 245
v80 = sub_7525E0(qword_E7FF28, v23, v20, v30);
dword_126E4A8 = v80;
if (v80 == -1) {
    sub_4F8420(2664);  // emit error 2664: "invalid or no value specified with --nv_arch flag"
    assert_fail("proc_command_line", 12219);
}
sub_7525F0(v80);  // set_target_configuration
```

If the architecture value is -1 (the stub's return value), error 2664 fires. This means cudafe++ relies entirely on nvcc to provide a valid integer architecture code via the `--target` argument. The `--nv_arch` flag name appears in the error string at `0x8884F0` but has no direct xrefs -- it is an alias or legacy name for the same mechanism.

### set_target_configuration

After storing the SM version, `sub_7525F0` (`set_target_configuration`, `target.c:299`) performs post-parse initialization:

```c
// sub_7525F0
__int64 __fastcall sub_7525F0(int a1)
{
    if ((unsigned int)(a1 + 1) > 1)  // rejects -1 and below
        assert_fail("set_target_configuration", 299);
    sub_7515D0();           // type table allocation / init
    qword_126E1B0 = "lib";  // library search prefix
}
```

The guard `(a1 + 1) > 1` accepts any value >= 0, rejecting only -1 (the sentinel) and values below it. This is a sanity check -- `sub_7525E0` should never return -1 in production because nvcc always provides a valid target.

### SM Version Propagation

During translation unit initialization (`sub_586240`, called from `fe_translation_unit_init`), the SM version is copied into the TU descriptor:

```c
// sub_586240
dword_126EBF8 = dword_126E4A8;  // target_config_index = sm_architecture
```

After this point, architecture checks throughout the compiler read either `dword_126E4A8` (the CLI-level global) or `dword_126EBF8` (the TU-level copy). Both contain the same integer SM version code.

## Layer 2: CUDA Feature Thresholds

cudafe++ checks the SM architecture version at semantic analysis time to gate CUDA-specific features. When a feature is used on an architecture below its minimum requirement, the compiler emits a diagnostic error or warning. All thresholds below were extracted from error strings embedded in the binary and confirmed through cross-reference analysis.

### Complete Feature Threshold Table

| Feature | Min Architecture | Diagnostic Tag | Error String |
|---|---|---|---|
| Device variadic functions | compute_30 | `device_function_has_ellipsis` | `__device__ or __host__ __device__ function with ellipsis requires compute_30 or higher architecture` |
| `__managed__` variables | compute_30 | `unsupported_arch_for_managed_capability` | `__managed__ variables require architecture compute_30 or higher` |
| `alloca()` in device code | compute_52 | `alloca_unsupported_for_lower_than_arch52` | `alloca() is not supported for architectures lower than compute_52` |
| Atomic scope argument | sm_60 | `nv_atomic_operations_scope_fallback_to_membar` | `atomic operations' scope argument is supported on architecture sm_60 or above. Fall back to use membar.` |
| Atomic f64 add/sub | sm_60 | `nv_atomic_add_sub_f64_not_supported` | `atomic add and sub for 64-bit float is supported on architecture sm_60 or above.` |
| `__nv_atomic_*` functions | sm_60 | `nv_atomic_functions_not_supported_below_sm60` | `__nv_atomic_* functions are not supported on arch < sm_60.` |
| `__grid_constant__` | compute_70 | `grid_constant_unsupported_arch` | `__grid_constant__ annotation is only allowed for architecture compute_70 or later` |
| Atomic memory order | sm_70 | `nv_atomic_operations_memory_order_fallback_to_membar` | `atomic operations' argument of memory order is supported on architecture sm_70 or above. Fall back to use membar.` |
| 128-bit atomic load/store | sm_70 | `nv_atomic_load_store_b128_version_too_low` | `128-bit atomic load and store are supported on architecture sm_70 or above.` |
| 16-bit atomic CAS | sm_70 | `nv_atomic_cas_b16_not_supported` | `16-bit atomic compare-and-exchange is supported on architecture sm_70 or above.` |
| `__nv_register_params__` | compute_80 | `register_params_unsupported_arch` | `__nv_register_params__ is only supported for compute_80 or later architecture` |
| `__wgmma_mma_async` | sm_90a | `wgmma_mma_async_not_enabled` | `__wgmma_mma_async builtins are only available for sm_90a` |
| Atomic cluster scope | sm_90 | `nv_atomic_operations_scope_cluster_change_to_device` | `atomic operations' scope of cluster is supported on architecture sm_90 or above. Using device scope instead.` |
| 128-bit atomic exch/CAS | sm_90 | `nv_atomic_exch_cas_b128_not_supported` | `128-bit atomic exchange or compare-and-exchange is supported on architecture sm_90 or above.` |

### Diagnostic Behavior: Errors vs Warnings

Not all architecture gate violations produce hard errors. The system distinguishes three behaviors:

1. **Hard errors** -- Compilation stops. Features that fundamentally cannot work on the target architecture: `__managed__` below compute_30, `__grid_constant__` below compute_70, `__nv_register_params__` below compute_80, `__wgmma_mma_async` below sm_90a, `alloca()` below compute_52.

2. **Fallback warnings** -- Compilation continues with degraded behavior. Atomic operations with scope/memory-order arguments on pre-sm_60/sm_70 architectures fall back to `membar`-based implementations. The compiler emits a warning and generates functionally correct but potentially less performant code.

3. **Scope demotion warnings** -- Cluster scope atomics on pre-sm_90 architectures silently demote to device scope. The diagnostic is informational ("Using device scope instead").

### compute_XX vs sm_XX Naming

The error strings use two naming conventions: `compute_XX` for features that are checked at the virtual architecture level (relevant to PTX generation), and `sm_XX` for features tied to a specific physical architecture (relevant to SASS generation). In practice, cudafe++ stores a single integer in `dword_126E4A8` and the distinction is semantic -- both forms gate against the same numeric value. The `sm_90a` suffix (with the `a` accelerator flag) is a special case used only for `__wgmma_mma_async` builtins, which require the Hopper accelerated architecture variant.

### architecture_features.json Summary

The extracted `architecture_features.json` catalogs 6 feature groups across 4 SM versions:

| SM Version | Features Introduced |
|---|---|
| SM 30 | Managed memory (`__managed__`), device variadic functions |
| SM 60 | Atomic scope argument, 64-bit float atomics, `__nv_atomic_*` API |
| SM 70 | `__grid_constant__`, 128-bit atomic load/store, atomic memory order, 16-bit CAS |
| SM 90 | `__wgmma_mma_async`, thread block clusters, 128-bit atomic exchange/CAS, cluster scope |

Notably absent from the cudafe++ error strings are features like cooperative groups (sm_60+) and tensor cores (sm_70+), which are checked at runtime or by the PTX assembler (ptxas) rather than the language frontend.

## Layer 3: Host Compiler Version Gating

cudafe++ generates `.int.c` output that must compile cleanly under the host C++ compiler (GCC, Clang, or MSVC). Because different host compiler versions support different warning pragmas and language features, cudafe++ gates its output based on the host compiler version stored in `qword_126EF98` (GCC) and `qword_126EF90` (Clang).

### Version Encoding

Both GCC and Clang versions are encoded as a single integer: `major * 10000 + minor * 100 + patch`. The compiler tests these values against hexadecimal threshold constants using `>` (greater-than) comparisons, which effectively means "version strictly above threshold," or equivalently "version at or above threshold+1."

### Complete Threshold Table

| Hex Constant | Decimal | Encoded Version | Effective Gate |
|---|---|---|---|
| `0x75F7` | 30,199 | 3.01.99 | GCC/Clang >= 3.2 |
| `0x76BF` | 30,399 | 3.03.99 | GCC/Clang >= 3.4 |
| `0x7787` | 30,599 | 3.05.99 | Clang >= 3.6 |
| `0x78B3` | 30,899 | 3.08.99 | Clang >= 3.9 |
| `0x9C3F` | 39,999 | 3.99.99 | GCC >= 4.0 |
| `0x9D07` | 40,199 | 4.01.99 | GCC >= 4.2 |
| `0x9D6B` | 40,299 | 4.02.99 | GCC >= 4.3 |
| `0x9E97` | 40,599 | 4.05.99 | GCC >= 4.6 |
| `0x9EFB` | 40,699 | 4.06.99 | GCC >= 4.7 |
| `0x9F5F` | 40,799 | 4.07.99 | GCC >= 4.8 |
| `0xEA5F` | 59,999 | 5.99.99 | GCC >= 6.0 |
| `0xEB27` | 60,199 | 6.01.99 | GCC >= 6.2 |
| `0x1116F` | 69,999 | 6.99.99 | GCC >= 7.0 |
| `0x15F8F` | 89,999 | 8.99.99 | GCC/Clang >= 9.0 |
| `0x1D4BF` | 119,999 | 11.99.99 | GCC/Clang >= 12.0 |
| `0x222DF` | 139,999 | 13.99.99 | GCC >= 14.0 |

### How Thresholds Are Used

The thresholds serve two purposes:

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

**2. C++ feature gating during dialect resolution.** The post-parsing dialect resolution in `proc_command_line` uses `qword_126EF98` thresholds to decide which C++ language features to enable. For example, GCC versions prior to 4.3 lack support for variadic templates, so the variadic template flag is suppressed when the GCC version is below `0x9D6B` (40299).

**3. CUDA compatibility mode.** A special flag `dword_E7FF10` (`cuda_compat_flag`) is set when `dword_126EFAC && qword_126EF98 <= 0x76BF` -- that is, when extended features are enabled but the GCC version is 3.3.99 or below. This activates a legacy compatibility path for very old host compilers.

### The `0xEA5F` (59999) Threshold

The threshold `0xEA5F` (GCC 6.0) is one of the most widely used in the binary, appearing in over a dozen decompiled functions. It gates several C++ feature behaviors:

```c
// From sub_40D900 (declaration processing)
if (dword_126EFA4 || (dword_126EFA8 && qword_126EF98 > 0xEA5F))
    // Enable feature X (Clang always, GCC only 6.0+)

// From sub_48C710 (initializer processing)
if (dword_126EFAC && !dword_126EFA4 && qword_126EF98 > 0xEA5F)
    // Extended feature with GNU extensions but not Clang, GCC 6.0+
```

The pattern `dword_126EFA4 || qword_126EF98 > 0xEA5F` translates to "Clang (any version) OR GCC 6.0+," which corresponds to the C++14/17 feature set. GCC 6.0 was the first GCC release with full C++14 support and substantial C++17 support.

### Dialect Initialization Flow

The host compiler version globals are initialized in `proc_command_line` and propagated to the dialect system during TU initialization:

```
proc_command_line (CLI parsing):
  case 184 (--gnu_version=X):  qword_126EF98 = X   // GCC version
  case 188 (--clang_version=X): qword_126EF90 = X   // Clang version

dialect_init (TU setup, sub_740000 range):
  if (gnu_mode):
    dword_126E1F8 = 1           // host dialect = GNU
    qword_126E1F0 = qword_126EF98  // host version = GCC version
  if (clang_mode):
    dword_126E1E8 = 1           // device dialect = Clang
    qword_126E1E0 = qword_126EF90  // device version = Clang version
```

## The --db Debug Mechanism

The `--db` flag (CLI case 37) activates EDG's internal debug tracing system by calling `sub_48A390` (`proc_debug_option`). While not directly related to architecture gating, the `--db` mechanism shares the same global namespace (`dword_126EFC8`, `dword_126EFCC`) and is relevant because debug tracing can expose the architecture checks as they execute.

### proc_debug_option (sub_48A390)

This 238-line function (`debug.c`) parses debug control strings of the form `"name=level"`:

```c
// CLI dispatch
case 37:  // --db=<string>
    if (sub_48A390(qword_E7FF28))  // proc_debug_option
        goto error;
    dword_106C2A0 = dword_126EFCC;  // save initial error count baseline
```

The debug option string is parsed as follows:

| Input Format | Action |
|---|---|
| Numeric only (e.g., `"5"`) | Sets `dword_126EFCC` (debug verbosity level) |
| `#name` or `-name` | Removes matching entry from trace request list |
| `name=level` | Adds trace request: action type 1, with specified level |
| `name+=level` | Adds trace request: action type 2 (additive) |
| `name-=level` | Adds trace request: action type 3 (subtractive) |
| `level!` suffix | Marks trace request as permanent (survives resets) |

Debug trace requests are stored as a linked list at `qword_1065870`. Each node is 28 bytes:

```
struct debug_trace_node {
    void*    next;           // +0:  linked list link
    char*    name_string;    // +8:  entity name to trace
    int32_t  action_type;    // +16: 1=set, 2=add, 3=subtract
    int32_t  level;          // +20: trace level
    int32_t  permanent;      // +24: survives reset if set
};
```

When `dword_126EFC8` (debug tracing enabled) is nonzero, the compiler emits trace output at architecture check points, IL allocation calls, template instantiation steps, and other key operations. The verbosity level `dword_126EFCC` controls how much detail is emitted: `>2` = detailed, `>3` = very detailed, `>4` = IL walk trace with entry kinds.

### db_name (CLI case 190)

The `--db_name` flag (case 190) calls a separate function `sub_48AD80` to register a debug name filter, restricting trace output to entities matching the specified name pattern.

## Three-Layer Checking Model

The architecture feature gating in cudafe++ operates as a three-layer system where each layer catches different classes of violations at different stages of compilation:

### Layer 1: Compile-Time Semantic Checks (cudafe++ Frontend)

These are the primary gates. During semantic analysis, cudafe++ reads `dword_126E4A8` and compares it against threshold constants. Violations emit diagnostic errors through the standard error system (diagnostic IDs in the 3000+ range, displayed as 20000-series via the `+16543` offset formula). These checks are unconditional -- they fire regardless of whether the code would actually execute at runtime.

**Enforcement point:** Declaration processing, type checking, and CUDA-specific semantic validation passes.

**Examples:**
- `__managed__` variable declaration with `dword_126E4A8 < 30` triggers error `unsupported_arch_for_managed_capability`
- `__grid_constant__` parameter with `dword_126E4A8 < 70` triggers error `grid_constant_unsupported_arch`
- `__wgmma_mma_async` call with insufficient arch triggers error `wgmma_mma_async_not_enabled`

### Layer 2: String-Embedded Diagnostic Checks

Error strings with architecture names baked into `.rodata` represent the complete set of architecture-dependent diagnostics. These strings are loaded by the diagnostic system and formatted with the current architecture value. The strings serve as the user-visible feedback for Layer 1 checks.

The architecture name in the string (e.g., "compute_70", "sm_90a") is a literal constant, not a formatted parameter -- the compiler does not interpolate the actual target architecture into these messages. This means the error messages always state the *minimum required* architecture, not what the user actually specified.

### Layer 3: Host Compiler Version Gating

This layer does not check GPU architecture at all -- instead, it gates the *output format* of the generated `.int.c` file based on the host C++ compiler's version. The thresholds ensure that GCC/Clang-specific pragmas, attributes, and language constructs in the generated code are compatible with the actual host compiler that will consume the output.

**Enforcement point:** Backend code generation (`sub_489000` and related functions in `cp_gen_be.c`).

**Impact:** Incorrect host compiler version gating does not cause compilation failure -- it may produce warnings from the host compiler due to unrecognized pragmas, or miss warning suppression directives that would silence spurious diagnostics.

### Interaction Between Layers

```
User Code
  |
  v
[Layer 1: cudafe++ semantic analysis]
  - Checks dword_126E4A8 against SM thresholds
  - Emits CUDA-specific errors for unsupported features
  |
  v
[Layer 2: Diagnostic formatting]
  - Loads error string with baked-in architecture name
  - Formats and displays error to user
  |
  v
[Layer 3: .int.c code generation]
  - Checks qword_126EF98 / qword_126EF90 for host compiler version
  - Emits appropriate #pragma directives
  - Generates host-compiler-compatible boilerplate
  |
  v
Host Compiler (gcc/clang/msvc)
```

Layers 1 and 2 operate during the frontend phase and can halt compilation. Layer 3 operates during the backend phase and only affects the format of the generated output file.

## Global Variable Summary

| Address | Size | Name | Role |
|---|---|---|---|
| `dword_126E4A8` | 4 | `sm_architecture` | Target SM version from `--target` (case 245). Initialized to -1 by `sub_752C90`. |
| `dword_126EBF8` | 4 | `target_config_index` | TU-level copy of `dword_126E4A8`. |
| `qword_126EF98` | 8 | `gcc_version` | GCC compatibility version. Default 80100 (8.1.0). Set by `--gnu_version` (case 184). |
| `qword_126EF90` | 8 | `clang_version` | Clang compatibility version. Default 90100 (9.1.0). Set by `--clang_version` (case 188). |
| `dword_126EFA8` | 4 | `gcc_extensions` | GCC mode enabled (set by `--gcc`). |
| `dword_126EFA4` | 4 | `clang_extensions` | Clang mode enabled (set by `--clang`). |
| `dword_126EFAC` | 4 | `extended_features` | Extended features / GNU compat mode. |
| `dword_126EFB0` | 4 | `gnu_extensions_enabled` | GNU extensions active. |
| `dword_126E1F8` | 4 | `host_dialect_gnu` | Host compiler is GCC/GNU. |
| `dword_126E1E8` | 4 | `host_dialect_clang` | Host compiler is Clang. |
| `qword_126E1F0` | 8 | `host_gcc_version` | Host GCC version (copied from `qword_126EF98`). |
| `qword_126E1E0` | 8 | `host_clang_version` | Host Clang version (copied from `qword_126EF90`). |
| `dword_126EFC8` | 4 | `debug_trace_enabled` | Debug tracing active (set by `--db`). |
| `dword_126EFCC` | 4 | `debug_verbosity` | Debug output level. >2=detailed, >4=IL walk trace. |
| `dword_E7FF10` | 4 | `cuda_compat_flag` | Legacy compat mode (`dword_126EFAC && qword_126EF98 <= 0x76BF`). |

## Cross-References

- [CLI Flag Inventory](../config/cli-flags.md) -- `--target`, `--gnu_version`, `--clang_version`, `--db` flag details
- [CUDA Error Catalog](../diagnostics/cuda-errors.md) -- Complete diagnostic messages for each feature gate
- [.int.c File Format](../output/int-c-format.md) -- Host compiler pragma emission details
- [Backend Code Generation](../pipeline/backend.md) -- GCC/Clang version threshold usage in output
- [Global Variable Index](../reference/global-variables.md) -- Full address-level documentation
- [Architecture Detection](../config/arch-detection.md) -- `--target` flag and SM version parsing details
