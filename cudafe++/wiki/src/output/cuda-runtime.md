# CUDA Runtime Boilerplate

Every `.int.c` file emitted by cudafe++ contains a fixed block of CUDA runtime initialization code, injected unconditionally before the main body. This boilerplate implements lazy initialization of the CUDA managed memory runtime and defines macro stubs for the extended lambda detection system. The managed runtime block is always emitted regardless of whether the translation unit uses `__managed__` variables -- the static flag `__nv_inited_managed_rt` ensures the runtime is initialized at most once, and the `static` linkage prevents symbol conflicts across translation units. The lambda detection macros provide a compile-time protocol between cudafe++ and `crt/host_runtime.h`: the runtime header inspects these macros to decide whether to compile lambda wrapper infrastructure.

## Key Facts

| Property | Value |
|---|---|
| Emitter function | `sub_489000` (`process_file_scope_entities`, line 218) |
| Managed RT string address | `0x83AAC8` (243 bytes) |
| Init function string address | `0x83ABC0` (210 bytes) |
| Managed access wrapper string | `0x839570` (65 bytes) |
| Access wrapper emitters | `sub_4768F0` (`gen_name_ref`, xref at `0x476DCF`), `sub_484940` (`gen_variable_name`, xref at `0x484A08`) |
| Lambda stub macros string | `0x83AD10`, `0x83AD50`, `0x83AD98` |
| Lambda existence check string | `0x83ADE8` (194 bytes) |
| Extended lambda mode flag | `dword_106BF38` (`extended_lambda_mode`) |
| Alternative host flag | `dword_106BF6C` (`alternative_host_compiler_mode`) |
| `__cudaPushCallConfiguration` lookup | `sub_511D40` (`scan_expr_full`), string at `0x899213` |
| Push config error message | `0x88CA48`, error code 3654 |
| Managed variable detection | `(*(_WORD *)(entity + 148) & 0x101) == 0x101` |
| EDG source file | `cp_gen_be.c` |

## Managed Memory Runtime Initialization

### Static Variables Block

The first emission at line 218 of `sub_489000` outputs four declarations as a single string literal:

```c
static char __nv_inited_managed_rt = 0;
static void **__nv_fatbinhandle_for_managed_rt;
static void __nv_save_fatbinhandle_for_managed_rt(void **in) {
    __nv_fatbinhandle_for_managed_rt = in;
}
static char __nv_init_managed_rt_with_module(void **);
```

These are emitted verbatim from a single string at `0x83AAC8`:

```text
"static char __nv_inited_managed_rt = 0; static void **__nv_fatbinhandle_for_managed_rt;
 static void __nv_save_fatbinhandle_for_managed_rt(void **in)
 {__nv_fatbinhandle_for_managed_rt = in;} static char __nv_init_managed_rt_with_module(void **);"
```

Each component serves a specific role:

| Symbol | Type | Purpose |
|---|---|---|
| `__nv_inited_managed_rt` | `static char` | Guard flag: 0 = not initialized, nonzero = initialized |
| `__nv_fatbinhandle_for_managed_rt` | `static void**` | Cached fatbinary handle, set during `__cudaRegisterFatBinary` |
| `__nv_save_fatbinhandle_for_managed_rt` | `static void (void**)` | Stores the fatbin handle for later use by the init function |
| `__nv_init_managed_rt_with_module` | `static char (void**)` | Forward declaration -- defined by `crt/host_runtime.h` |

The forward declaration of `__nv_init_managed_rt_with_module` is critical: this function is provided by the CUDA runtime headers (`crt/host_runtime.h`) and performs the actual CUDA runtime API calls to register managed variables with the unified memory system. By forward-declaring it here, the managed runtime boilerplate can reference it before the header is `#include`d later in the file.

### Lazy Initialization Function

Immediately after the static block, `sub_489000` emits the `__nv_init_managed_rt` inline function. The emission has a conditional prefix:

```c
// sub_489000, decompiled lines 221-224
if (dword_106BF6C)   // alternative host compiler mode
    emit("__attribute__((unused)) ");

emit(" static inline void __nv_init_managed_rt(void) {"
     " __nv_inited_managed_rt = (__nv_inited_managed_rt"
     " ? __nv_inited_managed_rt"
     "                 : __nv_init_managed_rt_with_module("
     "__nv_fatbinhandle_for_managed_rt));}");
```

When `dword_106BF6C` (alternative host compiler mode) is set, the function is prefixed with `__attribute__((unused))` to suppress "defined but not used" warnings on host compilers that do not understand CUDA semantics.

The emitted function, reformatted for readability:

```c
static inline void __nv_init_managed_rt(void) {
    __nv_inited_managed_rt = (
        __nv_inited_managed_rt
            ? __nv_inited_managed_rt
            : __nv_init_managed_rt_with_module(
                  __nv_fatbinhandle_for_managed_rt)
    );
}
```

This is a lazy initialization pattern. On first call, `__nv_inited_managed_rt` is 0 (falsy), so the ternary takes the false branch and calls `__nv_init_managed_rt_with_module`. That function performs CUDA runtime registration and returns a nonzero value which is stored back into `__nv_inited_managed_rt`. On subsequent calls, the ternary short-circuits and returns the existing value without re-initializing. The function is `static inline` to allow the host compiler to inline it at every managed variable access site, and `static` to avoid symbol collisions across translation units.

### Runtime Registration Flow

The complete managed memory initialization sequence spans the compilation pipeline:

```text
1. cudafe++ emits __nv_save_fatbinhandle_for_managed_rt() definition
2. cudafe++ emits forward decl of __nv_init_managed_rt_with_module()
3. cudafe++ emits __nv_init_managed_rt() with lazy init pattern
4. #include "crt/host_runtime.h" provides __nv_init_managed_rt_with_module()
5. __cudaRegisterFatBinary() calls __nv_save_fatbinhandle_for_managed_rt()
   to cache the fatbin handle
6. First access to any __managed__ variable triggers __nv_init_managed_rt()
7. __nv_init_managed_rt_with_module() calls __cudaRegisterManagedVariable()
   for every __managed__ variable in the TU
```

## Managed Variable Access Transformation

When the backend encounters a reference to a `__managed__` variable during code generation, it wraps the access in a comma-operator expression that forces lazy initialization. This transformation is performed by two functions:

- `sub_4768F0` (`gen_name_ref`, xref at `0x476DCF`) -- handles qualified name references
- `sub_484940` (`gen_variable_name`, xref at `0x484A08`) -- handles direct variable name emission

### Detection Condition

Both functions detect `__managed__` variables using the same bitfield test:

```c
// sub_484940, decompiled line 11
if ((*(_WORD *)(entity + 148) & 0x101) == 0x101)
```

This tests two bits simultaneously as a 16-bit word read at offset 148:

| Byte | Bit | Mask | Meaning |
|---|---|---|---|
| `+148` | bit 0 | `0x01` | `__device__` memory space |
| `+149` | bit 0 | `0x01` (reads as `0x100` in word) | `__managed__` flag |

The combined mask `0x101` matches when both `__device__` and `__managed__` are set. The `__managed__` attribute handler (`sub_40E0D0`, `apply_nv_managed_attr`) always sets both bits: `__managed__` implies the variable resides in device global memory (`__device__`), with the additional unified-memory semantics.

### Emitted Wrapper

When the condition matches, the emitter outputs a prefix string from `0x839570`:

```text
(*( (__nv_inited_managed_rt ? (void)0: __nv_init_managed_rt()), (
```

After the variable name is emitted normally, the suffix `)))` closes the expression. The complete transformed access for a managed variable `managed_var` becomes:

```c
(*( (__nv_inited_managed_rt ? (void)0 : __nv_init_managed_rt()), (managed_var)))
```

Breaking down the expression:

1. **Outer `*(...)`** -- dereferences the result (the managed variable is accessed through a pointer after initialization)
2. **Comma operator `(init_expr, (managed_var))`** -- evaluates the init expression for its side effect, then yields the variable
3. **Ternary `__nv_inited_managed_rt ? (void)0 : __nv_init_managed_rt()`** -- lazy init guard: if already initialized, the ternary evaluates to `(void)0` (no-op). Otherwise, calls `__nv_init_managed_rt()` which performs runtime registration

This pattern guarantees that any access to any `__managed__` variable triggers runtime initialization exactly once, regardless of access order. The comma operator ensures the initialization is a sequenced side effect evaluated before the variable access.

### sub_4768F0 (gen_name_ref) -- Qualified Access Path

The name reference generator at `sub_4768F0` handles the more complex case where the variable access includes scope qualification (`::`, template arguments, member access):

```c
// sub_4768F0, decompiled lines 160-163
if (!v7 && a3 == 7 && (*(_WORD *)(v9 + 148) & 0x101) == 0x101) {
    v13 = 1;  // flag: need closing )))
    emit("(*( (__nv_inited_managed_rt ? (void)0: __nv_init_managed_rt()), (");
    // ... emit qualified name with scope resolution ...
}
```

The condition `a3 == 7` indicates the entity is a variable (IL entry kind 7). The `!v7` check (`v7 = a4`, the fourth parameter) gates on whether the access is from a context that already handles initialization. The `v13` flag tracks whether the closing `)))` needs to be emitted after the complete name expression:

```c
// sub_4768F0, decompiled lines 231-236
if (v13) {
    emit(")))");
    return 1;
}
```

### sub_484940 (gen_variable_name) -- Direct Access Path

The direct variable name emitter at `sub_484940` follows the same pattern but with a simpler structure:

```c
// sub_484940, decompiled lines 10-15
v1 = 0;
if ((*(_WORD *)(a1 + 148) & 0x101) == 0x101) {
    v1 = 1;  // flag: need closing )))
    emit("(*( (__nv_inited_managed_rt ? (void)0: __nv_init_managed_rt()), (");
}

// ... emit variable name (possibly anonymous, templated, etc.) ...

if (v1) {
    emit(")))");
    return;
}
```

This function handles three variable name forms:

1. **Thread-local variables** (byte `+163` bit 7 set) -- emits `"this"` string (4 characters via inline loop)
2. **Anonymous variables** (byte `+165` bit 2 set) -- dispatches to `sub_483A80` for generated name emission
3. **Regular variables** -- dispatches to `sub_472730` (`gen_expression_or_name`, mode 7)

The managed wrapper is applied around all three forms.

## __cudaPushCallConfiguration Lookup

When cudafe++ processes a CUDA kernel launch expression (`kernel<<<grid, block, shmem, stream>>>(args...)`), the frontend must locate the `__cudaPushCallConfiguration` runtime function to lower the `<<<>>>` syntax into standard C++ function calls. This lookup occurs in `sub_511D40` (`scan_expr_full`), the 80KB expression scanner.

### Lookup Mechanism

At case `0x48` (decimal 72, the token for kernel launch `<<<`), the scanner performs a name lookup:

```c
// sub_511D40, decompiled lines 1999-2006
sub_72EEF0("__cudaPushCallConfiguration", 0x1B);   // inject name into scope
v206 = sub_698940(v255, 0);                         // lookup the declaration

if (!v206 || *(_BYTE *)(v206 + 80) != 11) {        // not found or not a function
    sub_4F8200(0x0B, 3654, &qword_126DD38);         // emit error 3654
}
```

The lookup calls `sub_72EEF0` to insert the identifier `__cudaPushCallConfiguration` (27 bytes, `0x1B`) into the current scope context, then `sub_698940` performs the actual name resolution. If the declaration is not found (`!v206`) or the entity at offset `+80` is not a function (kind `!= 11`), error 3654 is emitted.

### Error 3654

The error string at `0x88CA48`:

```text
unable to find __cudaPushCallConfiguration declaration.
CUDA toolkit installation may be corrupt.
```

This error indicates that the CUDA runtime headers have not been properly included or that the toolkit installation is broken. The `__cudaPushCallConfiguration` function is declared in `crt/device_runtime.h` (included transitively through `crt/host_runtime.h`), so this error should only appear if the include paths are misconfigured.

The error is emitted with severity `0x0B` (11), which maps to a fatal error -- compilation cannot continue without this function because every kernel launch depends on it.

### Kernel Launch Lowering

After successful lookup, the scanner builds an AST node representing the lowered kernel launch. The `<<<grid, block, shmem, stream>>>` syntax is transformed into:

```c
// Conceptual lowering:
if (__cudaPushCallConfiguration(grid, block, shmem, stream) != 0) {
    // launch configuration failed
}
kernel(args...);
```

Error 3655 (emitted at line 2019) handles the case where the call configuration push succeeds syntactically but the stream argument is missing in contexts that require it. The string for this is `"explicit stream argument not provided in kernel launch"`.

## Lambda Detection Macros

### Default Stub Macros (No Extended Lambdas)

When `dword_106BF38` (`extended_lambda_mode`) is 0, `sub_489000` emits three macro definitions that evaluate to `false`, followed by an existence check:

```c
// sub_489000, decompiled lines 259-264
emit("#define __nv_is_extended_device_lambda_closure_type(X) false\n");
emit("#define __nv_is_extended_host_device_lambda_closure_type(X) false\n");
emit("#define __nv_is_extended_device_lambda_with_preserved_return_type(X) false\n");
emit("#if defined(__nv_is_extended_device_lambda_closure_type)"
     " && defined(__nv_is_extended_host_device_lambda_closure_type)"
     "&& defined(__nv_is_extended_device_lambda_with_preserved_return_type)\n"
     "#endif\n");
```

Verbatim emitted code:

```c
#define __nv_is_extended_device_lambda_closure_type(X) false
#define __nv_is_extended_host_device_lambda_closure_type(X) false
#define __nv_is_extended_device_lambda_with_preserved_return_type(X) false
#if defined(__nv_is_extended_device_lambda_closure_type) && defined(__nv_is_extended_host_device_lambda_closure_type)&& defined(__nv_is_extended_device_lambda_with_preserved_return_type)
#endif
```

Note the missing space before `&&` in the second conjunction -- this is exactly how the string appears in the binary at `0x83ADE8`. The `#if defined(...)` block is a compile-time assertion: if any of the three macros were `#undef`'d by a misbehaving header between this point and their use in `crt/host_runtime.h`, the preprocessor would silently skip lambda-related code rather than producing cryptic template errors. The `#endif` immediately follows -- the block has no body because its purpose is solely the existence check.

These macros are consumed by `crt/host_runtime.h` to conditionally compile lambda wrapper infrastructure. When all three evaluate to `false`, the runtime header skips device lambda wrapper template instantiation, host-device lambda wrapper instantiation, and trailing-return-type lambda handling.

### Trait-Based Macros (Extended Lambdas Active)

When `dword_106BF38` is nonzero (`--extended-lambda` or `--expt-extended-lambda` CLI flag), the stub macros are NOT emitted. Instead, the lambda preamble emitter `sub_6BCC20` (`nv_emit_lambda_preamble`) provides trait-based implementations later in the file body. The decision is made at line 256 of `sub_489000`:

```c
// sub_489000, decompiled lines 251-264
if (dword_106BF38)        // extended lambdas enabled?
    goto LABEL_38;        // skip stub macros, jump to next section
// else: emit stubs
emit("#define __nv_is_extended_device_lambda_closure_type(X) false\n");
// ...
```

The trait-based implementations emitted by `sub_6BCC20` use template specialization rather than preprocessor macros. Each macro is `#define`'d to invoke a type trait helper:

**Device lambda detection** (string at `0xA82CF8`):

```cpp
template <typename T>
struct __nv_extended_device_lambda_trait_helper {
  static const bool value = false;
};
template <typename T1, typename...Pack>
struct __nv_extended_device_lambda_trait_helper<__nv_dl_wrapper_t<T1, Pack...> > {
  static const bool value = true;
};
#define __nv_is_extended_device_lambda_closure_type(X) \
    __nv_extended_device_lambda_trait_helper< \
        typename __nv_lambda_trait_remove_cv<X>::type>::value
```

**Preserved return type detection** (string at `0xA82F68`):

```cpp
template <typename T>
struct __nv_extended_device_lambda_with_trailing_return_trait_helper {
  static const bool value = false;
};
template <typename U, U func, typename Return, unsigned Id, typename...Pack>
struct __nv_extended_device_lambda_with_trailing_return_trait_helper<
    __nv_dl_wrapper_t<__nv_dl_trailing_return_tag<U, func, Return, Id>, Pack...> > {
  static const bool value = true;
};
#define __nv_is_extended_device_lambda_with_preserved_return_type(X) \
    __nv_extended_device_lambda_with_trailing_return_trait_helper< \
        typename __nv_lambda_trait_remove_cv<X>::type >::value
```

**Host-device lambda detection** (string at `0xA831B0`):

```cpp
template <typename>
struct __nv_extended_host_device_lambda_trait_helper {
  static const bool value = false;
};
template <bool B1, bool B2, bool B3, typename T1, typename T2, typename...Pack>
struct __nv_extended_host_device_lambda_trait_helper<
    __nv_hdl_wrapper_t<B1, B2, B3, T1, T2, Pack...> > {
  static const bool value = true;
};
#define __nv_is_extended_host_device_lambda_closure_type(X) \
    __nv_extended_host_device_lambda_trait_helper< \
        typename __nv_lambda_trait_remove_cv<X>::type>::value
```

All three trait helpers follow the same pattern: a primary template with `value = false`, a partial specialization matching the corresponding wrapper type with `value = true`, and a macro that instantiates the trait after stripping cv-qualifiers via `__nv_lambda_trait_remove_cv`. The cv-stripping is necessary because lambda closure types may be captured as `const` references.

### Macro Registration in the Frontend

The three macro names are registered as built-in identifiers by `sub_5863A0` (a frontend initialization function), which calls `sub_7463B0` to register each name with a unique identifier code:

```c
// sub_5863A0, decompiled lines 976-978
sub_7463B0(328, "__nv_is_extended_device_lambda_closure_type");
sub_7463B0(329, "__nv_is_extended_host_device_lambda_closure_type");
sub_7463B0(330, "__nv_is_extended_device_lambda_with_preserved_return_type");
```

These registrations (IDs 328, 329, 330) make the names known to the EDG lexer before any source code is parsed, ensuring they can be resolved during preprocessing even if no header has defined them yet.

## Diagnostic Suppression Scope

The managed runtime boilerplate is wrapped in a `#pragma GCC diagnostic push` / `pop` block to isolate its warning suppressions:

```c
#pragma GCC diagnostic push
#pragma GCC diagnostic ignored "-Wunused-variable"
#pragma GCC diagnostic ignored "-Wunused-function"

/* managed runtime declarations */

#pragma GCC diagnostic pop
```

The push/pop is emitted only when the host compiler supports it: Clang (`dword_126E1E8` set), or GCC version > 40599 (`qword_126E1F0 > 0x9E97` and `dword_106BF6C` not set). The suppressions are necessary because `__nv_inited_managed_rt` and `__nv_init_managed_rt` are `static` symbols that may never be referenced in translation units without `__managed__` variables, causing `-Wunused-variable` and `-Wunused-function` warnings.

## Global State Dependencies

| Global | Type | Meaning | Effect on Emission |
|---|---|---|---|
| `dword_106BF38` | int | `extended_lambda_mode` | 0: emit false stubs. Nonzero: skip stubs, `sub_6BCC20` provides traits |
| `dword_106BF6C` | int | `alternative_host_compiler_mode` | Adds `__attribute__((unused))` to `__nv_init_managed_rt` |
| `dword_126E1E8` | int | Host is Clang | Controls push/pop and extra suppressions |
| `dword_126E1F8` | int | Host is GCC | Controls push/pop version threshold |
| `qword_126E1F0` | int64 | GCC/Clang version number | > 0x9E97 (40599) for push/pop support |

## Function Map

| Address | Name | Role |
|---|---|---|
| `sub_489000` | `process_file_scope_entities` | Emits managed RT block and lambda macros |
| `sub_4768F0` | `gen_name_ref` | Wraps qualified managed variable accesses |
| `sub_484940` | `gen_variable_name` | Wraps direct managed variable accesses |
| `sub_511D40` | `scan_expr_full` | Looks up `__cudaPushCallConfiguration` for `<<<>>>` lowering |
| `sub_6BCC20` | `nv_emit_lambda_preamble` | Emits trait-based lambda detection macros |
| `sub_5863A0` | (frontend init) | Registers lambda macro names as built-in identifiers |
| `sub_467E50` | (emit string) | Primary string emission to output stream |
| `sub_72EEF0` | (inject identifier) | Inserts `__cudaPushCallConfiguration` into scope for lookup |
| `sub_698940` | (name lookup) | Resolves identifier to entity declaration |
| `sub_4F8200` | (emit error) | Error emission with severity and error code |

## Cross-References

- [.int.c File Format](./int-c-format.md) -- complete file structure showing where runtime boilerplate sits
- [Device Lambda Wrapper](../lambda/device-wrapper.md) -- `__nv_dl_wrapper_t` matched by trait macros
- [Host-Device Lambda Wrapper](../lambda/host-device-wrapper.md) -- `__nv_hdl_wrapper_t` matched by trait macros
- [Preamble Injection](../lambda/preamble-injection.md) -- `sub_6BCC20` emission of trait templates
- [Entity Node Layout](../structs/entity-node.md) -- byte +148/+149 memory space bitfield
- [\_\_managed\_\_ Variables](../attributes/managed-variables.md) -- attribute handler setting the 0x101 bits
- [Kernel Stub Generation](../cuda/kernel-stubs.md) -- device stub side of kernel launch lowering
- [Host Reference Arrays](./host-reference-arrays.md) -- registration tables that reference managed variables
