# Kernel Stub Generation

When cudafe++ generates the `.int.c` host translation of a CUDA source file, every `__global__` kernel function undergoes a critical transformation: the original kernel body is suppressed and replaced with a **device stub** — a lightweight host-callable wrapper that delegates to `cudaLaunchKernel`. This mechanism is how CUDA kernel launch syntax (`kernel<<<grid, block>>>(args)`) ultimately becomes a regular C++ function call that the host compiler can process. The stub generation logic lives entirely within `gen_routine_decl` (`sub_47BFD0`), a 1,831-line function in `cp_gen_be.c` that is the central code generator for all C++ function declarations and definitions. A secondary function, `gen_bare_name` (`sub_473F10`), handles the character-by-character emission of the `__wrapper__device_stub_` prefix into function names.

The stub mechanism operates in two passes controlled by a global toggle, `dword_1065850` (the `device_stub_mode` flag). The toggle fires at the top of `gen_routine_decl`, BEFORE the body-selection logic runs. Because the toggle is `dword_1065850 = (dword_1065850 == 0)`, it flips 0->1 on the first invocation. This means:

- **First invocation** (toggle 0->1): `dword_1065850 == 1` at decision points -> emits the `static` declaration with `cudaLaunchKernel` placeholder body, then recurses.
- **Recursive invocation** (toggle 1->0): `dword_1065850 == 0` at decision points -> emits the forwarding body that calls `__wrapper__device_stub_<name>`.

Both invocations wrap the original kernel body in `#if 0` / `#endif` so the host compiler never sees device code.

## Key Facts

| Property | Value |
|---|---|
| Source file | `cp_gen_be.c` (EDG 6.6 backend code generator) |
| Main generator | `sub_47BFD0` (`gen_routine_decl`, 1831 lines) |
| Bare name emitter | `sub_473F10` (`gen_bare_name`, 671 lines) |
| Stub prefix string | `"__wrapper__device_stub_"` at `0x839420` |
| Specialization prefix | `"__specialization_"` at `0x839960` |
| cudaLaunchKernel body | `"{ ::cudaLaunchKernel(0, 0, 0, 0, 0, 0);}"` at `0x839CB8` |
| Device-only dummy (ctor/dtor) | `"{int *volatile ___ = 0;"` at `0x839A3E` + `"::free(___);"` at `0x839A72` |
| Device-only dummy (__global__) | `"{int volatile ___ = 1;"` at `0x839A56` + `"::exit(___);"` at `0x839A80` |
| Stub mode flag | `dword_1065850` (global toggle) |
| Static template stub CLI flag | `-static-global-template-stub=true` |
| Parameter list generator | `sub_478900` (`gen_parameter_list`) |
| Scope qualifier emitter | `sub_474D60` (recursive namespace path) |
| Parameter name emitter | `sub_474BB0` (emit entity name for forwarding) |

## The Device Stub Mode Toggle

The entire stub generation mechanism hinges on a single global variable, `dword_1065850`. This flag acts as a modal switch: when set, all subsequent code generation for `__global__` functions produces the static stub variant rather than the forwarding body.

### Toggle Logic

The toggle occurs in `gen_routine_decl` at the point where the function's CUDA flags are inspected. The critical line from the decompiled binary:

```c
// sub_47BFD0, around decompiled line 553
// v3 = routine entity pointer, v8 = is_friend flag

__int64 flags = *(_QWORD *)(v3 + 176);

if ((flags & 0x40000002000000) == 0x40000002000000 && v8 != 1)
    dword_1065850 = dword_1065850 == 0;   // toggle: 0->1 or 1->0
```

The bitmask `0x40000002000000` encodes a combination of the `__global__` attribute and a linkage/definition flag in the entity's 8-byte flags field at offset `+176`. The condition requires BOTH bits set and the declaration must NOT be a friend declaration (`v8 != 1`). The toggle expression `dword_1065850 == 0` flips the flag: if it was 0, it becomes 1; if it was 1, it becomes 0.

This means `gen_routine_decl` is called **twice** for every `__global__` kernel. Crucially, the toggle fires at the TOP of the function, BEFORE the body emission logic:

1. **First call** (`dword_1065850 == 0` at entry -> toggled to `1`): All subsequent decision points see `dword_1065850 == 1`. Emits the `static` stub with `cudaLaunchKernel` placeholder body. Then recurses.
2. **Recursive call** (`dword_1065850 == 1` at entry -> toggled to `0`): All subsequent decision points see `dword_1065850 == 0`. Emits the forwarding stub body. Does NOT recurse (the flag is 0 at the end).

The self-recursion that drives the second call is explicit at the end of `gen_routine_decl`:

```c
// sub_47BFD0, decompiled line 1817-1821
if (dword_1065850) {
    qword_1065748 = (int64_t)v163;  // restore source sequence pointer
    return sub_47BFD0(v152, a2);     // recursive self-call
}
```

After emitting the static stub (first call), the self-recursion check at line 1817 fires because `dword_1065850 == 1`. The function restores the source sequence state and calls itself. In the recursive call, the toggle fires again (1->0), and the forwarding body is emitted with `dword_1065850 == 0`. At the end of the recursive call, `dword_1065850 == 0`, so no further recursion occurs.

## Stub Generation: The Forwarding Body

When `dword_1065850 == 0` and the entity has `__global__` annotation (byte `+182 & 0x40`) with a body (`byte +179 & 0x02`), `gen_routine_decl` emits a forwarding body instead of the original kernel implementation. This is the output produced by the recursive (second) invocation.

### Step-by-Step Emission

The forwarding body is assembled from multiple `sub_468190` (emit raw string) calls:

```c
// Condition: (byte[182] & 0x40) != 0 && (byte[179] & 2) != 0 && dword_1065850 == 0

// 1. Open brace
sub_468190("{");

// 2. Scope qualification (if kernel is in a namespace)
scope = *(v3 + 40);  // entity's enclosing scope
if (scope && byte_at(scope + 28) == 3) {       // scope kind 3 = namespace
    sub_474D60(*(scope + 32));   // recursively emit namespace::namespace::...
    sub_468190("::");
}

// 3. Emit "__wrapper__device_stub_" prefix
sub_468190("__wrapper__device_stub_");

// 4. Emit the original function name
sub_468190(*(char **)(v3 + 8));  // entity name string at offset +8
```

### Template Argument Emission

After the function name, template arguments must be forwarded. The logic branches on whether the function is an explicit template specialization (`v153`) or a non-template member of a template class:

**Case A: Explicit specialization** (`v153 != 0`) — uses the template argument list at entity offset `+224`:

```c
v135 = *(v3 + 224);  // template_args linked list
if (v135) {
    putc('<', stream);  // emit '<'
    do {
        arg_kind = byte_at(v135 + 8);
        if (arg_kind == 0) {
            // Type argument: emit type specifier + declarator
            sub_5FE8B0(v135[4], ...);   // gen_type_specifier
            sub_5FB270(v135[4], ...);   // gen_declarator
        } else if (arg_kind == 1) {
            // Value argument (non-type template param)
            sub_5FCAF0(v135[4], 1, ...); // gen_constant
        } else {
            // Template-template argument
            sub_472730(v135[4], ...);    // gen_template_arg
        }
        v135 = *v135;          // next in linked list
        separator = v135 ? ',' : '>';
        putc(separator, stream);
    } while (v135);
}
```

**Case B: Non-specialization** — template parameters from the enclosing class template are forwarded:

```c
// v162 = template parameter info from enclosing scope
v92 = v162[1];  // template parameter list
if (v92 && (byte_at(v92 + 113) & 2) == 0) {
    sub_467E50("<");
    do {
        param_kind = byte_at(v92 + 112);
        if (param_kind == 1) {
            // type parameter -- emit the type
            sub_5FE8B0(*(v92 + 120), ...);
            sub_5FB270(*(v92 + 120), ...);
        } else if (param_kind == 2) {
            // non-type parameter -- emit constant
            sub_5FCAF0(*(v92 + 120), 1, ...);
        } else {
            // template-template parameter
            sub_472730(*(v92 + 120), ...);
        }
        if (byte_at(v92 + 113) & 1)
            sub_467E50("...");   // parameter pack expansion
        v92 = *(v92 + 104);     // next parameter
        emit(v92 ? "," : ">");
    } while (v92);
}
```

### Parameter Forwarding

After the name and template arguments, the forwarding call's actual arguments are emitted:

```c
// 5. Emit parameter forwarding: "(param1, param2, ...)"
sub_468150(40);  // '('
param = *(v167 + 40);  // first parameter entity from definition scope
if (param) {
    for (separator = ""; ; separator = ",") {
        sub_468190(separator);
        sub_474BB0(param, 7);  // emit parameter name
        if (byte_at(param + 166) & 0x40) {
            sub_468190("...");  // variadic parameter pack expansion
        }
        param = *(param + 104);  // next parameter in list
        if (!param) break;
    }
}
sub_468190(");");

// 6. Emit return statement and closing brace
sub_468190("return;}");
```

### Complete Output Example

For a kernel:
```cuda
namespace my_ns {
template<typename T>
__global__ void my_kernel(T* data, int n) { /* device code */ }
}
```

The forwarding body (emitted during the recursive call with `dword_1065850 == 0`) produces:
```cpp
template<typename T>
void my_ns::my_kernel(T* data, int n) {
    my_ns::__wrapper__device_stub_my_kernel<T>(data, n);
    return;
}
#if 0
/* original kernel body here */
#endif
```

Note: `__host__` is NOT emitted in the forwarding body. The `__global__` attribute is stripped and no explicit execution space appears. The function appears as a plain C++ function in `.int.c`.

## Stub Generation: The Static cudaLaunchKernel Placeholder

When `dword_1065850 == 1` (the first invocation, after the toggle), the function declaration is rewritten with a different storage class and body. Despite being called "pass 2" conceptually (it produces the definition that the forwarding body calls), it is emitted FIRST in the output because the toggle sets the flag before any body emission logic runs.

### Declaration Modifiers

When `dword_1065850` is set, `gen_routine_decl` forces the storage class to `static` and optionally prepends the `__specialization_` prefix:

```c
// sub_47BFD0, decompiled lines 897-903
if (dword_1065850) {
    v164 = 2;                    // force storage class = static
    v23 = "static";
    if (v153)                    // if template specialization
        sub_467E50("__specialization_");
    goto emit_storage_class;     // -> sub_467E50("static"); sub_468150(' ');
}
```

The `__specialization_` prefix is emitted BEFORE `static` for template specializations. This creates names like `__specialization_static void __wrapper__device_stub_kernel(...)` which the CUDA runtime uses to distinguish specialization stubs from primary template stubs.

### Name Emission via gen_bare_name

In stub mode, `gen_bare_name` (`sub_473F10`) prepends the wrapper prefix character-by-character. The relevant code path:

```c
// sub_473F10, decompiled lines 130-144
if (byte_at(v2 + 182) & 0x40 && dword_1065850) {
    // Emit line directive if pending
    if (dword_1065818)
        sub_467DA0();

    // Character-by-character emission of "__wrapper__device_stub_"
    v25 = "_wrapper__device_stub_";   // note: starts at second char
    v26 = 95;                          // first char: '_' (0x5F = 95)
    do {
        ++v25;
        putc(v26, stream);
        v26 = *(v25 - 1);
        ++dword_106581C;
    } while ((char)v26);
}
```

The technique is notable: the string `"_wrapper__device_stub_"` is stored starting at the second character, and the first underscore (`_`, ASCII 95) is loaded as the initial character separately. The `do/while` loop then walks the string pointer forward, emitting each character via `putc` and incrementing the column counter (`dword_106581C`). This assembles the full `__wrapper__device_stub_` prefix before the actual function name is emitted.

### cudaLaunchKernel Placeholder Body

For non-specialization `__global__` kernels in stub mode, the body is a single-line placeholder:

```c
// sub_47BFD0, decompiled lines 1424-1429
if (dword_1065850) {
    if (!v153 && v90) {    // not a specialization AND has __global__ body
        sub_468190("{ ::cudaLaunchKernel(0, 0, 0, 0, 0, 0);}");
        goto suppress_original;
    }
}
```

The call `::cudaLaunchKernel(0, 0, 0, 0, 0, 0)` is never actually executed at runtime. It exists solely to create a linker dependency on the CUDA runtime library, ensuring that `cudaLaunchKernel` is linked even though the real launch is performed through the CUDA driver API. The six zero arguments match the signature `cudaError_t cudaLaunchKernel(const void*, dim3, dim3, void**, size_t, cudaStream_t)`.

### Complete Output Example (Static Stub)

For the same kernel above, the static stub (emitted first, with `dword_1065850 == 1`) produces:

```cpp
static void __wrapper__device_stub_my_kernel(float* data, int n) {
    ::cudaLaunchKernel(0, 0, 0, 0, 0, 0);
}
```

## Dummy Bodies for Non-Kernel Device Functions

Not all CUDA-annotated functions are `__global__` kernels. Device-only functions (constructors, destructors, and plain `__device__` functions) that have definitions also need host-side bodies to prevent host compiler errors. These receive **dummy bodies** designed to suppress optimizer warnings while remaining syntactically valid.

### Condition for Dummy Body Emission

The dummy body path activates in the ELSE branch of the `__global__` check — that is, for non-kernel device functions. The condition from the decompiled code (lines 1603-1606):

```c
// This path is reached when (byte[182] & 0x40) == 0 -- entity is NOT __global__
// The flags field at offset +176 is an 8-byte bitfield encoding linkage/definition state.

uint64_t flags = *(uint64_t*)(entity + 176);
if ((flags & 0x30000000000500) != 0x20000000000000)  // NOT a device-only entity with definition
    goto emit_original_body;                          // skip dummy, emit normally

if (!dword_106BFDC || (entity->byte_81 & 4) != 0)   // whole-program flag check
{
    // Emit dummy body for device-only function visible in host output
}
```

The bitmask `0x30000000000500` extracts the device-annotation and definition bits from the 8-byte flags field. The target value `0x20000000000000` selects entities that have device annotation set but no host-side definition — exactly the functions that need a dummy body to satisfy the host compiler.

### Constructor/Destructor Dummy (definition_kind 1 or 2)

For constructors (`definition_kind == 1`) and destructors (`definition_kind == 2`), the dummy body allocates a volatile null pointer and frees it:

```c
// sub_47BFD0, decompiled lines 1611-1651
if ((unsigned char)(byte[166] - 1) <= 1) {
    sub_468190("{int *volatile ___ = 0;");
    // ... emit (void)param; for each parameter ...
    sub_468190("::free(___);}");
}
```

Output:
```cpp
{int *volatile ___ = 0;(void)param1;(void)param2;::free(___);}
```

The `volatile` qualifier prevents the optimizer from removing the allocation. The `::free(0)` call is a no-op at runtime but establishes a dependency on the C library and prevents dead code elimination of the entire body.

### __global__ / Regular Device Function Dummy (definition_kind >= 3)

For non-constructor/destructor device functions, a different pattern is used:

```c
else {
    sub_468190("{int volatile ___ = 1;");
    // ... emit (void)param; for each parameter ...
    sub_468190("::exit(___);}");
}
```

Output:
```cpp
{int volatile ___ = 1;(void)param1;(void)param2;::exit(___);}
```

The `::exit(1)` call guarantees the function is never considered to "return normally" by the host compiler's control-flow analysis, suppressing missing-return-value warnings for non-void functions.

### Parameter Usage Emission

Between the opening and closing statements, each named parameter is referenced with `(void)param;` to suppress unused-parameter warnings. The loop walks the parameter list:

```c
for (kk = *(v167 + 40); kk; kk = *(kk + 104)) {
    if (*(kk + 8) && !(byte_at(kk + 166) & 0x40)) {  // has name, not a pack
        // For aggregate types with GNU host compiler: complex cast chain
        if (!dword_1065750 && dword_126E1F8
            && is_aggregate_type(*(kk + 112))
            && has_nontrivial_dtor(*(kk + 112))) {
            sub_468190("(void)");
            sub_468190("reinterpret_cast<void *>(&(const_cast<char &>");
            sub_468190("(reinterpret_cast<const volatile char &>(");
            sub_474BB0(kk, 7);  // parameter name
            sub_468190("))))");
        } else {
            sub_468190("(void)");
            sub_474BB0(kk, 7);  // parameter name
        }
        sub_468150(';');
    }
}
```

The complex `reinterpret_cast` chain for aggregate types with non-trivial destructors avoids triggering GCC/Clang warnings about taking the address of a parameter that might be passed in registers.

## The #if 0 / #endif Suppression

After the stub body is emitted, the original kernel body is wrapped in preprocessor guards to hide it from the host compiler:

```c
// sub_47BFD0, decompiled lines 1598-1601
sub_46BC80("#if 0");       // emit "#if 0\n"
--dword_1065834;           // decrease indent level
sub_467D60();              // emit newline

// ... then emit the original body via:
dword_1065850_saved = dword_1065850;
dword_1065850 = 0;                    // temporarily disable stub mode
sub_47AEF0(*(v167 + 80), 0);         // gen_statement_full: emit original body
dword_1065850 = dword_1065850_saved;  // restore stub mode
sub_466C10();                          // finalize

// ... then emit #endif
putc('#', stream);
// character-by-character emission of "#endif\n"
```

The function temporarily disables stub mode (`dword_1065850 = 0`) while emitting the original body so that any nested constructs are generated normally. After the body, `#endif` is emitted and stub mode is restored.

For definitions (when `v112 == 0`), a trailing `; ` is appended after `#endif` to satisfy host compilers that may expect a statement terminator.

## The -static-global-template-stub Flag

The CLI flag `-static-global-template-stub=true` controls how template `__global__` functions are stubbed. When enabled, template kernel stubs receive `static` linkage, which avoids ODR violations when the same template kernel is instantiated in multiple translation units during whole-program compilation (`-rdc=false`).

The flag produces two diagnostic messages when it encounters problematic patterns:

1. **Extern template kernel**: `"when "-static-global-template-stub=true", extern __global__ function template is not supported in whole program compilation mode ("-rdc=false")"` — An `extern` template kernel cannot receive a static stub because the definitions would conflict across TUs.

2. **Missing definition**: `"when "-static-global-template-stub=true" in whole program compilation mode ("-rdc=false"), a __global__ function template instantiation or specialization (%sq) must have a definition in the current translation unit"` — The static stub requires a local definition to replace.

Both diagnostics recommend either switching to `-rdc=true` (separate compilation) or explicitly setting `-static-global-template-stub=false`.

## Diagnostic Push/Pop Around Stubs

Before emitting device stub declarations, `gen_routine_decl` wraps the output in compiler-specific diagnostic suppression to prevent spurious warnings:

For GCC/Clang hosts (`dword_126E1F8` set, version > `0x9E97` = 40599):
```c
sub_467E50("\n#pragma GCC diagnostic push\n");
sub_467E50("#pragma GCC diagnostic ignored \"-Wunused-parameter\"\n");
// ... stub emission ...
sub_467E50("\n#pragma GCC diagnostic pop\n");
```

For MSVC hosts (`dword_126E1D8` set):
```c
sub_467E50("\n__pragma(warning(push))\n");
sub_467E50("__pragma(warning(disable : 4100))\n");  // unreferenced formal parameter
// ... stub emission ...
sub_467E50("\n__pragma(warning(pop))\n");
```

For static template specialization stubs, an additional warning is suppressed:
- GCC/Clang: `#pragma GCC diagnostic ignored "-Wunused-function"` (warning 4505 on MSVC: "unreferenced local function has been removed")

## Deferred Function List for Whole-Program Mode

When `dword_106BFBC` (a whole-program compilation flag) is set and `dword_106BFDC` is clear, instead of emitting a dummy body immediately, `gen_routine_decl` adds the function to a deferred list:

```c
// sub_47BFD0, decompiled lines 1713-1745
v117 = sub_6B7340(32);          // allocate 32-byte node
v117[0] = qword_1065840;        // link to previous head
v117[1] = source_start;         // source position start
v117[2] = source_end;           // source position end
if (has_name)
    v117[3] = strdup(name);     // copy of function name
else
    v117[3] = NULL;
qword_1065840 = v117;           // push onto list head
```

This deferred list (`qword_1065840`) is later consumed during the breakpoint placeholder generation phase in `process_file_scope_entities` (`sub_489000`), where each deferred entry produces a `static __attribute__((used)) void __nv_breakpoint_placeholder<N>_<name>(void) { exit(0); }` function.

## Function Map

| Address | Name | Role |
|---|---|---|
| `sub_47BFD0` | `gen_routine_decl` | Main stub generator; 1831 lines; handles all function declarations |
| `sub_473F10` | `gen_bare_name` | Character-by-character name emission with `__wrapper__device_stub_` prefix |
| `sub_474BB0` | `gen_entity_name` | Parameter name emission for forwarding calls |
| `sub_474D60` | `gen_scope_qualifier` | Recursive namespace path emission (`ns1::ns2::`) |
| `sub_478900` | `gen_parameter_list` | Parameter list with type transformation in stub mode |
| `sub_478D70` | `gen_function_declarator_with_scope` | Full function declarator with cv-qualifiers and ref-qualifiers |
| `sub_47AEF0` | `gen_statement_full` | Statement generator used for emitting original body inside `#if 0` |
| `sub_47ECC0` | `gen_template` / `process_source_sequence` | Top-level dispatch; also sets `dword_1065850` for instantiation directives |
| `sub_46BC80` | (emit #if directive) | Emits `#if 0` / `#if 1` preprocessor lines |
| `sub_467E50` | (emit string) | Primary string emission to output stream |
| `sub_468190` | (emit raw string) | Raw string emission (no line directive) |
| `sub_489000` | `process_file_scope_entities` | Backend entry point; consumes deferred function list |

## Concrete Example: Simple Kernel Stub Output

Given this input CUDA source:

```cuda
__global__ void add_one(int *data, int n) {
    int idx = threadIdx.x + blockIdx.x * blockDim.x;
    if (idx < n)
        data[idx] += 1;
}
```

cudafe++ generates the following in the `.int.c` host translation file. The toggle fires at the top of `gen_routine_decl` (0->1), so the static stub definition is emitted FIRST, followed by the forwarding body from the recursive call.

### Output 1: Static Stub Definition (first call, dword_1065850 == 1 after toggle)

The static stub provides the linker symbol that the forwarding body calls. Diagnostic pragmas wrap the declaration to suppress unused-parameter warnings:

```cpp
#pragma GCC diagnostic push
#pragma GCC diagnostic ignored "-Wunused-parameter"
static void __wrapper__device_stub_add_one(int *data, int n) {
    ::cudaLaunchKernel(0, 0, 0, 0, 0, 0);
}
#if 0
/* Original kernel body -- hidden from host compiler */
{
    int idx = threadIdx.x + blockIdx.x * blockDim.x;
    if (idx < n)
        data[idx] += 1;
}
#endif
#pragma GCC diagnostic pop
```

The `static` storage class is forced by the check at decompiled line 897-903. The `__wrapper__device_stub_` prefix is emitted by `gen_bare_name` (`sub_473F10`). The `cudaLaunchKernel` placeholder body comes from the string literal at `0x839CB8`.

### Output 2: Forwarding Body (recursive call, dword_1065850 == 0 after toggle)

After the static stub is emitted and `gen_routine_decl` recurses, the forwarding body replaces the original kernel body. The `__global__` attribute is stripped (kernels become regular host functions in `.int.c`):

```cpp
void add_one(int *data, int n) {__wrapper__device_stub_add_one(data, n);return;}
#if 0
/* Original kernel body -- hidden from host compiler (emitted again) */
{
    int idx = threadIdx.x + blockIdx.x * blockDim.x;
    if (idx < n)
        data[idx] += 1;
}
#endif
```

The forwarding body is assembled character-by-character:
1. `{` — open brace
2. Scope qualifier (none for file-scope kernels; `ns::` for namespaced ones)
3. `__wrapper__device_stub_` — the stub prefix from string at `0x839420`
4. `add_one` — the original function name from `entity + 8`
5. `(data, n)` — parameter names forwarded (no types, just names via `sub_474BB0`)
6. `);return;}` — close the forwarding call and return

The original body appears in `#if 0` in both outputs because both code paths reach the same `LABEL_457` -> `sub_46BC80("#if 0")` emission point.

### Template Kernel Example

For a template kernel:

```cuda
template<typename T>
__global__ void scale(T *data, T factor, int n) { /* ... */ }

// explicit instantiation
template __global__ void scale<float>(float *, float, int);
```

Output 1 (first call, dword_1065850 == 1) produces a specialization stub:

```cpp
__specialization_static void __wrapper__device_stub_scale(float *data, float factor, int n) {
    ::cudaLaunchKernel(0, 0, 0, 0, 0, 0);
}
```

Output 2 (recursive call, dword_1065850 == 0) produces a forwarding stub with template arguments:

```cpp
template<typename T>
void scale(T *data, T factor, int n) {__wrapper__device_stub_scale<T>(data, factor, n);return;}
```

The `__specialization_` prefix is emitted only when the entity is a template specialization (`v153 != 0`) and `dword_1065850` is set (decompiled line 901-902).

### Device-Only Function Example

For a non-kernel `__device__` function with a body:

```cuda
__device__ int device_helper(int x, int y) {
    return x + y;
}
```

The host output uses a dummy body instead of a forwarding stub (since there is no `__wrapper__device_stub_` target for non-kernel functions):

```cpp
__attribute__((unused)) int device_helper(int x, int y) {int volatile ___ = 1;(void)x;(void)y;::exit(___);}
#if 0
{
    return x + y;
}
#endif
```

The `__attribute__((unused))` prefix is emitted when the function's execution space is device-only (`(byte_182 & 0x70) == 0x20`) and `dword_126E1F8` (GCC host compiler mode) is set (decompiled line 905-906).

## Cross-References

- [Execution Spaces](./execution-spaces.md) — byte `+182` bitfield that drives the `__global__` check; complete redeclaration matrix
- [Device/Host Separation](./device-host-separation.md) — IL marking that determines which functions need stubs; the `dword_1065850` toggle lifecycle
- [RDC Mode](./rdc-mode.md) — separate compilation mode that affects stub linkage
- [.int.c File Format](../output/int-c-format.md) — overall structure of the generated host file
- [CUDA Runtime Boilerplate](../output/cuda-runtime.md) — managed memory initialization emitted alongside stubs
