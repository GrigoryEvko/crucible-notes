# JIT Mode

JIT mode is a compilation mode where cudafe++ produces **device code only** -- no host `.int.c` file, no kernel stubs, no CUDA runtime registration tables. The output is a standalone device IL payload suitable for runtime compilation via NVRTC (`nvrtcCompileProgram`) or direct loading through the CUDA Driver API (`cuModuleLoadData`, `cuModuleLoadDataEx`). Because there is no host compiler invocation downstream, anything that belongs exclusively to the host side is illegal: explicit `__host__` functions, unannotated functions (which default to `__host__`), namespace-scope variables without memory-space qualifiers, non-const class static data members, and lambda closures inferred to have `__host__` execution space.

The `--default-device` flag inverts the annotation default -- unannotated entities become `__device__` instead of `__host__`, allowing C++ code written without CUDA annotations to compile directly for the GPU. This is the recommended workaround for all four unannotated-entity diagnostics.

## Key Facts

| Property | Value |
|---|---|
| Compilation output | Device IL only (no `.int.c`, no stubs, no registration) |
| Host output suppression | `--gen_c_file_name` (flag 45) not supplied by driver |
| Device output path | `--gen_device_file_name` (flag 85) |
| Default execution space (normal) | `__host__` (entity+182 byte == `0x00`) |
| Default execution space (JIT + `--default-device`) | `__device__` (entity+182 byte `0x23`) |
| Annotation override flag | `--default-device` (passed to cudafe++ by NVRTC or nvcc) |
| RDC mode flag | `--device-c` (flag 77) -- relocatable device code; orthogonal to JIT |
| JIT diagnostic count | 5 error messages (1 explicit-host + 4 unannotated-entity) |
| Diagnostic tag suffix | All five tags end with `_in_jit` |
| NVRTC integration | NVRTC calls cudafe++ with JIT-appropriate flags internally |
| Driver API consumers | `cuModuleLoadData`, `cuModuleLoadDataEx`, `cuLinkAddData` |

## How JIT Mode Is Activated

cudafe++ is never invoked directly by application code. In the standard offline compilation pipeline, nvcc invokes cudafe++ with both `--gen_c_file_name` (flag 45, the host `.int.c` path) and `--gen_device_file_name` (flag 85, the device IL path). Both outputs are generated from a single frontend invocation -- cudafe++ uses a single-pass architecture internally (see [Device/Host Separation](../cuda/device-host-separation.md)).

In JIT mode, the driving tool -- typically NVRTC -- invokes cudafe++ with **only** the device-side output path. The host-output file name (`--gen_c_file_name`) is not provided, so no `.int.c` file is generated. The absence of a host output target is what structurally makes this "JIT mode": without a host file, there is no host compiler to feed, and therefore no host-side constructs can be tolerated.

### Activation Conditions

JIT mode is not a single user-facing CLI flag. It is an internal compilation state activated by the combination of flags that the driving tool (nvcc or NVRTC) sets when invoking cudafe++:

1. **NVRTC invocation.** NVRTC always invokes cudafe++ in JIT mode. NVRTC compiles CUDA C++ source to PTX at application runtime. There is no host compiler, no host object file, and no linking -- the output is pure device code.

2. **nvcc `--ptx` or `--cubin` without host compilation.** When nvcc is asked to produce only PTX or cubin output (no host object), it may invoke cudafe++ with the JIT mode configuration to skip host-side generation entirely.

3. **Architecture target combined with device-only flags.** The internal JIT state is set when the target configuration (`--target`, flag 245 -> `dword_126E4A8`) is combined with device-only compilation flags (e.g., `--device-syntax-only`, flag 72).

The practical effect: when JIT mode is active, the entire implicit-host-annotation system becomes a source of errors rather than a convenience. Every function without `__device__` or `__global__` defaults to `__host__`, and host entities are illegal.

### NVRTC Runtime Compilation Path

NVRTC (`libnvrtc.so` / `nvrtc64_*.dll`) is NVIDIA's runtime compilation library. Application code calls `nvrtcCreateProgram` with CUDA C++ source text, then `nvrtcCompileProgram` to compile it. Internally, NVRTC embeds a complete CUDA compilation pipeline including cudafe++ and cicc, invoking them with JIT-appropriate flags:

```text
Application
    |
    v
nvrtcCompileProgram(prog, numOptions, options)
    |
    v
cudafe++ --target <sm_code> --gen_device_file_name <tmpfile> [--default-device] ...
    |                    (no --gen_c_file_name => JIT mode)
    v
cicc <tmpfile> --> PTX
    |
    v
ptxas / cuModuleLoadData --> device binary (cubin)
```

The user-facing NVRTC options (`--gpu-architecture=compute_90`, `--device-debug`, etc.) are translated by the NVRTC library into internal cudafe++ and cicc flags. The `--default-device` flag is passed through when the user includes it in the NVRTC options array.

### CUDA Driver API Consumption

The PTX or cubin produced by the JIT pipeline is consumed by the CUDA Driver API:

- **`cuModuleLoadData` / `cuModuleLoadDataEx`**: Load a compiled module (PTX or cubin) into the current context. The driver JIT-compiles PTX to native binary at load time.
- **`cuLinkAddData` / `cuLinkComplete`**: Link multiple compiled objects into a single module (JIT linking for RDC workflows).
- **`cuModuleGetFunction`**: Retrieve a `__global__` kernel handle from the loaded module for launch via `cuLaunchKernel`.

Because JIT-compiled code has no host-side registration (no `__cudaRegisterFunction` calls, no fatbin embedding), the Driver API is the only path to launch kernels from JIT-compiled modules. The CUDA Runtime API launch syntax (`<<<>>>`) is not available for JIT-compiled kernels -- the application must use `cuLaunchKernel` explicitly.

## The --default-device Flag

In normal (offline) compilation, functions and namespace-scope variables without explicit CUDA annotations default to `__host__`. This default makes sense when both host and device outputs are generated: the unannotated entities go into the host `.int.c` file and are compiled by the host compiler.

In JIT mode, this default is counterproductive. Most code intended for JIT compilation targets the GPU, and requiring explicit `__device__` on every function and variable is verbose and incompatible with header-only libraries written for standard C++.

The `--default-device` flag changes the default:

| Entity type | Default without `--default-device` | Default with `--default-device` |
|---|---|---|
| Unannotated function | `__host__` (entity+182 == `0x00`) | `__device__` (entity+182 == `0x23`) |
| Namespace-scope variable (no memory space) | Host variable | `__device__` variable (entity+148 bit 0 set) |
| Non-const class static data member | Host variable | `__device__` variable |
| Lambda closure class (namespace scope) | `__host__` inferred space | `__device__` inferred space |
| Explicitly `__host__` function | `__host__` (unchanged) | `__host__` (unchanged -- always error in JIT) |
| Explicitly `__device__` function | `__device__` (unchanged) | `__device__` (unchanged) |
| `__global__` kernel | `__global__` (unchanged) | `__global__` (unchanged) |

Entities with explicit annotations are unaffected. Only entities that would otherwise receive the implicit `__host__` default are redirected to `__device__`.

### Interaction with Entity+182

The execution-space bitfield at entity+182 (documented in [Execution Spaces](../cuda/execution-spaces.md)) is set during attribute application. Without `--default-device`, an unannotated function has byte `0x00` at entity+182 -- the `0x30` mask extracts `0x00`, which is treated as implicit `__host__`. With `--default-device` active, the frontend treats unannotated functions as if `__device__` had been applied, setting byte+182 to `0x23` (the standard `__device__` OR mask: `device_capable | device_explicit | device_annotation`).

This means the downstream subsystems -- keep-in-IL marking, cross-space validation, device-only filtering -- all see a properly-annotated `__device__` entity and process it identically to an explicitly annotated one. The flag does not add a "JIT mode" code path through every subsystem; it simply changes the default annotation, and the existing execution-space machinery handles the rest.

### How to Pass the Flag

In normal nvcc workflows, `--default-device` is passed through `-Xcudafe`:

```bash
nvcc -Xcudafe --default-device source.cu
```

In NVRTC workflows, the flag is passed via the `nvrtcCompileProgram` options array:

```c
const char *opts[] = {"--default-device"};
nvrtcCompileProgram(prog, 1, opts);
```

## JIT Mode Diagnostics

Five error messages enforce JIT mode restrictions. All five are emitted during semantic analysis when the frontend encounters an entity that cannot exist in a device-only compilation. The messages are self-documenting: four of the five include an explicit suggestion to use `--default-device`.

### Diagnostic 1: Explicit __host__ Function

**Tag:** `no_host_in_jit`

**Message:**
```text
A function explicitly marked as a __host__ function is not allowed in JIT mode
```

**Trigger:** The function declaration carries an explicit `__host__` annotation (entity+182 has bit 4 set via the `0x15` OR mask from `apply_nv_host_attr` at `sub_4108E0`). This is unconditionally illegal in JIT mode -- there is no device-side representation of a host-only function, and JIT mode produces no host output.

**No `--default-device` suggestion:** This is the only JIT diagnostic that does not suggest `--default-device`. The flag only affects unannotated entities. An explicit `__host__` annotation overrides the default. The fix must be a source code change: remove `__host__`, change it to `__device__`, or change it to `__host__ __device__`.

**Example:**
```cuda
// JIT mode: error no_host_in_jit
__host__ void setup() { /* ... */ }

// Fix options:
__device__ void setup() { /* ... */ }
__host__ __device__ void setup() { /* ... */ }  // if needed in both contexts
```

### Diagnostic 2: Unannotated Function

**Tag:** `unannotated_function_in_jit`

**Message:**
```text
A function without execution space annotations (__host__/__device__/__global__)
is considered a host function, and host functions are not allowed in JIT mode.
Consider using -default-device flag to process unannotated functions as __device__
functions in JIT mode
```

**Trigger:** A function entity has `(entity+182 & 0x30) == 0x00` -- no explicit execution-space annotation. By default this means implicit `__host__`, which is illegal in JIT mode.

**Fix:** Either add `__device__` to the function declaration, or compile with `--default-device`.

**Example:**
```cuda
// JIT mode without --default-device: error unannotated_function_in_jit
int compute(int x) { return x * x; }

// Fix 1: explicit annotation
__device__ int compute(int x) { return x * x; }

// Fix 2: compile with --default-device (function becomes implicitly __device__)
```

### Diagnostic 3: Unannotated Namespace-Scope Variable

**Tag:** `unannotated_variable_in_jit`

**Message:**
```text
A namespace scope variable without memory space annotations
(__device__/__constant__/__shared__/__managed__) is considered a host variable,
and host variables are not allowed in JIT mode. Consider using -default-device flag
to process unannotated namespace scope variables as __device__ variables in JIT mode
```

**Trigger:** A variable declared at namespace scope (including global scope and anonymous namespaces) lacks a CUDA memory-space annotation. In normal compilation, such variables live in host memory. In JIT mode, host memory is inaccessible.

The check applies to the memory-space bitfield at entity+148, not the execution-space bitfield at entity+182. Without any annotation, none of the memory-space bits (`__device__` bit 0, `__shared__` bit 1, `__constant__` bit 2, `__managed__` bit 3) are set.

**Scope note:** This check targets namespace-scope variables only. Local variables inside `__device__` or `__global__` functions are not subject to this check -- they live on the device stack or in registers.

**Fix:** Add a memory-space annotation, or compile with `--default-device`.

**Example:**
```cuda
// JIT mode without --default-device: error unannotated_variable_in_jit
int table[256] = { /* ... */ };

// Fix 1: mutable device memory
__device__ int table[256] = { /* ... */ };

// Fix 2: read-only data
__constant__ int table[256] = { /* ... */ };
```

### Diagnostic 4: Non-Const Class Static Data Member

**Tag:** `unannotated_static_data_member_in_jit`

**Message:**
```text
A class static data member with non-const type is considered a host variable,
and host variables are not allowed in JIT mode. Consider using -default-device flag
to process such data members as __device__ variables in JIT mode
```

**Trigger:** A class or struct has a `static` data member whose type is not `const`-qualified. Static data members are allocated at namespace scope (not per-instance), so they are subject to the same host-variable prohibition as namespace-scope variables.

**Why non-const only:** `const` and `constexpr` static members with compile-time-constant initializers can be folded into device code by cicc without requiring an actual global variable in host memory. Non-const static members require mutable storage that must be explicitly placed in device memory.

**Example:**
```cuda
struct Config {
    // JIT mode without --default-device: error unannotated_static_data_member_in_jit
    static int max_iterations;

    // OK: const with constant initializer (compile-time folding)
    static const int default_value = 42;

    // OK: constexpr (compile-time constant)
    static constexpr float pi = 3.14159f;
};

// Fix: explicit annotation
struct Config {
    __device__ static int max_iterations;
};
```

### Diagnostic 5: Lambda Closure Class with Inferred __host__ Space

**Tag:** `host_closure_class_in_jit`

**Message:**
```text
The execution space for the lambda closure class members was inferred to be __host__
(based on context). This is not allowed in JIT mode. Consider using -default-device
to infer __device__ execution space for namespace scope lambda closure classes.
```

**Trigger:** A lambda expression at namespace scope (or in a context where the enclosing function has implicit `__host__` space) produces a closure class whose execution space is inferred to be `__host__`. The lambda was not explicitly annotated with `__device__`, and the enclosing context is host-only, so cudafe++'s execution-space inference assigns `__host__` to the closure class members.

This diagnostic interacts with the extended lambda system (documented in [Extended Lambda Overview](../lambda/overview.md)). In normal compilation, a namespace-scope lambda without annotations is host-only and gets a closure type compiled for the CPU. In JIT mode, that closure type has no valid compilation target.

**Fix:** Either annotate the lambda with `__device__` (requires extended lambdas: `--expt-extended-lambda`), or pass `--default-device` to change the inference to `__device__`.

**Example:**
```cuda
// JIT mode without --default-device: error host_closure_class_in_jit
auto fn = [](int x) { return x * 2; };

// Fix 1: explicit annotation (requires --expt-extended-lambda)
auto fn = [] __device__ (int x) { return x * 2; };

// Fix 2: compile with --default-device
```

## Diagnostic Summary

| Tag | Entity type | `--default-device` suggested | Suppressible |
|---|---|---|---|
| `no_host_in_jit` | Explicit `__host__` function | No | Yes (via `--diag_suppress`) |
| `unannotated_function_in_jit` | Function with no annotation | Yes | Yes |
| `unannotated_variable_in_jit` | Namespace-scope variable, no annotation | Yes | Yes |
| `unannotated_static_data_member_in_jit` | Non-const static data member | Yes | Yes |
| `host_closure_class_in_jit` | Lambda closure inferred `__host__` | Yes | Yes |

All five diagnostics use the standard cudafe++ diagnostic system. They can be controlled via CLI flags or source pragmas:

```bash
--diag_suppress=unannotated_function_in_jit
--diag_warning=no_host_in_jit
#pragma nv_diag_suppress unannotated_variable_in_jit
```

**Warning:** Suppressing these diagnostics silences the messages but does not change the underlying problem. The entities still have host execution space and will be absent from the device IL output, leading to link errors or runtime failures when the module is loaded.

## Architecture: JIT Mode vs Normal Mode

| Aspect | Normal (offline) mode | JIT mode |
|---|---|---|
| Driver tool | nvcc | NVRTC (or nvcc with `--ptx` / `--cubin`) |
| Host output (`.int.c`) | Generated via `sub_489000` | Not generated |
| Device IL output | Generated via keep-in-IL walk | Generated via keep-in-IL walk (identical) |
| Kernel stubs | `__wrapper__device_stub_` in `.int.c` | Not needed |
| Registration code | `__cudaRegisterFunction` / `__cudaRegisterVar` | Not emitted |
| Fatbin embedding | Embedded in host object | Not applicable |
| Default unannotated space | `__host__` | `__host__` (error) or `__device__` (with `--default-device`) |
| Kernel launch mechanism | `<<<>>>` -> `cudaLaunchKernel` (Runtime API) | `cuLaunchKernel` (Driver API) |
| Module loading | Automatic (CUDA runtime startup) | Manual (`cuModuleLoadData`) |
| Link model | Static linking with host object | JIT linking (`cuLinkAddData`) or direct load |

### Single-Pass Architecture Impact

cudafe++ uses a single-pass architecture: the EDG frontend parses the source once, builds a unified IL tree, and tags every entity with execution-space bits at entity+182. In normal mode, two output filters run on this tree -- one for the host `.int.c` file (driven by `sub_489000` -> `sub_47ECC0`), one for the device IL (driven by the keep-in-IL walk at `sub_610420`). In JIT mode, only the device IL output path runs. The host output path is simply never invoked because no host output was requested.

This means JIT mode does not require a fundamentally different code path through the frontend. Parsing, semantic analysis, template instantiation, and IL construction all proceed identically. The difference manifests at two points:

1. **Diagnostic emission during semantic analysis.** The five JIT diagnostics fire when the frontend detects entities that would be host-only. In normal mode, these entities are silently accepted because they will appear in the host output.

2. **Output generation.** The backend skips host-file emission entirely. The keep-in-IL walk runs as usual, marking device-reachable entries with bit 7 of the prefix byte (`entry_ptr - 8`). The device IL writer produces the binary output. No stub generation (`gen_routine_decl` stub path), no registration table emission, no `.int.c` formatting.

## Interaction with Other Modes

### RDC (Relocatable Device Code)

JIT mode is orthogonal to RDC (`--device-c`, flag 77). RDC controls whether device code is compiled for separate linking (enabling cross-TU `__device__` function calls and `extern __device__` variables), while JIT mode controls whether host output is produced. Both can be active simultaneously -- for example, NVRTC with `--relocatable-device-code=true` compiles device code for separate device linking without any host output.

When RDC is combined with JIT mode, NVRTC compiles each source file to relocatable device code, and the driver-API linker (`cuLinkAddData`, `cuLinkComplete`) resolves cross-references at load time. Without RDC, all device code must be self-contained within a single translation unit.

### Extended Lambdas

Extended lambdas (`--expt-extended-lambda`, controlled by `dword_106BF38`) interact with JIT mode through the lambda closure class inference. The `host_closure_class_in_jit` diagnostic targets the case where a lambda's closure is inferred as host-side. With `--default-device`, the inference changes to device-side, resolving the conflict. Extended lambda capture rules still apply in JIT mode -- captures must be trivially device-copyable, subject to the 1023-capture limit, and array captures are limited to 7 dimensions.

### Relaxed Constexpr

Relaxed constexpr mode (`--expt-relaxed-constexpr`, flag 104, sets `dword_106BFF0`) makes constexpr functions implicitly `__host__ __device__`. In JIT mode, this resolves many unannotated-function errors because constexpr functions gain the `__device__` annotation implicitly via the HD bypass (entity+177 bit 4). However, non-constexpr unannotated functions still trigger `unannotated_function_in_jit` unless `--default-device` is also active.

## Practical Patterns

### Pattern 1: Minimal JIT Kernel

```cuda
// Source passed to nvrtcCreateProgram -- no --default-device needed
extern "C" __global__ void add(float* a, float* b, float* c, int n) {
    int i = blockIdx.x * blockDim.x + threadIdx.x;
    if (i < n) c[i] = a[i] + b[i];
}
```

No annotations needed beyond `__global__` on the kernel. All code within the kernel body is implicitly device code. The `extern "C"` prevents name mangling so the kernel can be found by `cuModuleGetFunction`.

### Pattern 2: JIT-Compiling Library Code with --default-device

```cuda
// Header-only math library, no CUDA annotations
template <typename T>
T clamp(T val, T lo, T hi) {
    return val < lo ? lo : (val > hi ? hi : val);
}

__global__ void kernel(float* data, int n) {
    int i = blockIdx.x * blockDim.x + threadIdx.x;
    if (i < n) data[i] = clamp(data[i], 0.0f, 1.0f);
}
```

Without `--default-device`, `clamp` triggers `unannotated_function_in_jit`. With `--default-device`, `clamp` is implicitly `__device__` and compiles cleanly.

### Pattern 3: Guarding Host Code with Preprocessor

```cuda
// Use __CUDACC_RTC__ to guard host-only code
#ifndef __CUDACC_RTC__
__host__ void cpu_fallback(float* data, int n) {
    for (int i = 0; i < n; i++) data[i] *= 2.0f;
}
#endif

__global__ void gpu_process(float* data, int n) {
    int i = blockIdx.x * blockDim.x + threadIdx.x;
    if (i < n) data[i] *= 2.0f;
}
```

`__CUDACC_RTC__` is predefined by NVRTC. Code guarded by `#ifndef __CUDACC_RTC__` is invisible to the JIT compiler, avoiding `no_host_in_jit` errors.

### Pattern 4: Static Data Members in JIT

```cuda
struct Constants {
    static constexpr int BLOCK_SIZE = 256;        // OK: constexpr, folded at compile time
    static const float EPSILON;                    // Error without --default-device (non-constexpr const)
};

#ifdef __CUDACC_RTC__
__device__
#endif
const float Constants::EPSILON = 1e-6f;            // Annotated for JIT mode
```

## Function Map

| Address | Name | Lines | Role |
|---|---|---|---|
| `sub_459630` | `proc_command_line` | 4105 | CLI parser; processes `--default-device` and `--device-c` flags |
| `sub_452010` | `init_command_line_flags` | 3849 | Registers all flags including `default-device` |
| `sub_610420` | `mark_to_keep_in_il` | 892 | Device IL marking (runs identically in JIT and normal mode) |
| `sub_489000` | `process_file_scope_entities` | 723 | Host `.int.c` backend (skipped entirely in JIT mode) |
| `sub_47ECC0` | `gen_template` | 1917 | Source-sequence dispatcher; host output path (skipped in JIT) |
| `sub_40EB80` | `apply_nv_device_attr` | 100 | Sets `__device__` bits; entity+182 OR `0x23` (function), entity+148 OR `0x01` (variable) |
| `sub_4108E0` | `apply_nv_host_attr` | 31 | Sets `__host__` bits; entity+182 OR `0x15` |

## Cross-References

- [Execution Spaces](../cuda/execution-spaces.md) -- entity+182 bitfield, `__host__`/`__device__`/`__global__` OR masks, `0x30` mask classification
- [Device/Host Separation](../cuda/device-host-separation.md) -- single-pass architecture, keep-in-IL walk, host/device output file generation
- [Cross-Space Validation](../cuda/cross-space-validation.md) -- execution-space call checking (still applies in JIT mode for HD entities)
- [CUDA Error Catalog](../diagnostics/cuda-errors.md) -- Category 10 (JIT Mode), all five diagnostic messages with tag names
- [CLI Flag Inventory](../config/cli-flags.md) -- flag table, `--gen_device_file_name` (85), `--gen_c_file_name` (45), `--device-c` (77)
- [Architecture Feature Gating](../cuda/arch-gating.md) -- `--target` SM code (`dword_126E4A8`) and feature thresholds
- [Extended Lambda Overview](../lambda/overview.md) -- lambda closure class execution-space inference, wrapper types
- [Kernel Stubs](../cuda/kernel-stubs.md) -- `__wrapper__device_stub_` mechanism (absent in JIT mode)
- [RDC Mode](../cuda/rdc-mode.md) -- relocatable device code, separate compilation for device-side linking
