# Device Lambda Wrapper (`__nv_dl_wrapper_t`)

When a C++ lambda is annotated `__device__` inside CUDA code compiled with `--extended-lambda`, the closure class that the frontend creates has host linkage only -- it cannot be instantiated on the device. The device lambda wrapper system solves this by replacing the lambda expression at the call site with a construction of `__nv_dl_wrapper_t<Tag, F1, ..., FN>`, a template struct whose type parameters encode the lambda's identity (via `Tag`) and whose fields store the captured variables in device-accessible storage. The wrapper struct has a dummy `operator()` that never executes real code on the device side -- its purpose is purely to carry captured state across the host/device boundary. The actual device-side call is dispatched through the tag type, which encodes a function pointer to the lambda's `operator()` as a non-type template parameter.

Two tag types exist. `__nv_dl_tag` is the standard tag for lambdas with auto-deduced return types. `__nv_dl_trailing_return_tag` handles lambdas with explicit trailing return types, preserving the user-specified return type through the wrapper. Both tag types carry the lambda's `operator()` function pointer and a unique ID as template parameters.

The wrapper template does not exist in any header file. It is synthesized as raw C++ text by `sub_6BB790` (`emit_device_lambda_wrapper_specialization`) in `nv_transforms.c` and injected into the compilation stream during preamble emission. Only the capture counts actually used in the translation unit are emitted, controlled by a 1024-bit bitmap at `unk_1286980`.

## Key Facts

| Property | Value |
|---|---|
| Wrapper type | `__nv_dl_wrapper_t<Tag, CapturedVarTypePack...>` |
| Standard tag | `__nv_dl_tag<U, func, unsigned>` |
| Trailing-return tag | `__nv_dl_trailing_return_tag<U, func, Return, unsigned>` |
| Specialization emitter | `sub_6BB790` (`emit_device_lambda_wrapper_specialization`, 191 lines) |
| Per-lambda emission | `sub_47B890` (`gen_lambda`, 336 lines, `cp_gen_be.c`) |
| Preamble master emitter | `sub_6BCC20` (`nv_emit_lambda_preamble`, 244 lines) |
| Capture bitmap | `unk_1286980` (128 bytes = 1024 bits, device lambda) |
| Bitmap setter | `sub_6BCBF0` (`nv_record_capture_count`, 13 lines) |
| Max supported captures | 1024 |
| Source file | `nv_transforms.c` (specialization emitter), `cp_gen_be.c` (per-lambda call) |
| Field type trait | `__nv_lambda_field_type<T>` |

## Primary Template and Zero-Capture Specialization

The primary template is a static_assert trap -- any instantiation with a non-zero variadic pack that was not explicitly specialized triggers a compilation error. The zero-capture specialization (Tag only, no F parameters) provides a trivial constructor and a dummy `operator()` returning 0.

This code is emitted verbatim as a single string literal from `sub_6BCC20`:

```cpp
// Exact binary string (emitted as a single a1() call in sub_6BCC20):
template <typename Tag,typename...CapturedVarTypePack>
struct __nv_dl_wrapper_t {
static_assert(sizeof...(CapturedVarTypePack) == 0,"nvcc internal error: unexpected number of captures!");
};
template <typename Tag>
struct __nv_dl_wrapper_t<Tag> {
__nv_dl_wrapper_t(Tag) { }
template <typename...U1>
int operator()(U1...) { return 0; }
};
```

Note: no space after the comma in `Tag,typename...` and no indentation -- this is the literal text injected into the `.int.c` output. The primary template and the zero-capture specialization are emitted as a single string literal.

The primary template's `static_assert` acts as a safety net: if the frontend records a capture count of N but fails to emit the corresponding N-capture specialization, the host compiler will produce a diagnostic rather than silently generating broken code. The zero-capture specialization's `operator()` returns `int(0)` -- this value is never used at runtime because the device compiler dispatches through the tag's encoded function pointer, not through the wrapper's `operator()`.

## Tag Types

### `__nv_dl_tag`

The standard device lambda tag. Three template parameters encode the lambda identity. Exact binary string:

```cpp
template <typename U, U func, unsigned>
struct __nv_dl_tag { };
```

The string is `"\ntemplate <typename U, U func, unsigned>\nstruct __nv_dl_tag { };\n"` -- note the leading newline.

| Parameter | Role |
|---|---|
| `U` | Type of the lambda's `operator()` (deduced via `decltype`) |
| `func` | Non-type template parameter: pointer to the lambda's `operator()` |
| `unsigned` | Unnamed parameter: unique ID disambiguating lambdas with identical operator types |

The `__NV_LAMBDA_WRAPPER_HELPER(X, Y)` macro (emitted at preamble start) expands to `decltype(X), Y`, providing the `U, func` pair from a single expression. The full macro and helper text emitted as the first `a1()` call:

```cpp
#define __NV_LAMBDA_WRAPPER_HELPER(X, Y) decltype(X), Y
template <typename T>
struct __nvdl_remove_ref { typedef T type; };

template<typename T>
struct __nvdl_remove_ref<T&> { typedef T type; };

template<typename T>
struct __nvdl_remove_ref<T&&> { typedef T type; };

template <typename T, typename... Args>
struct __nvdl_remove_ref<T(&)(Args...)> {
  typedef T(*type)(Args...);
};

template <typename T>
struct __nvdl_remove_const { typedef T type; };

template <typename T>
struct __nvdl_remove_const<T const> { typedef T type; };
```

The `__nvdl_remove_ref` specialization for function references (`T(&)(Args...)`) is notable: it converts a function reference type to a function pointer type (`T(*)(Args...)`). This handles the case where a lambda captures a function by reference -- the wrapper field needs a copyable function pointer, not a reference.

### `__nv_dl_trailing_return_tag`

For lambdas with explicit trailing return types (`-> ReturnType`), a separate tag preserves the return type:

```cpp
template <typename U, U func, typename Return, unsigned>
struct __nv_dl_trailing_return_tag { };
```

The additional `Return` parameter carries the user-specified return type. This is necessary because the wrapper's `operator()` must return this type rather than `int`, and the body uses `__builtin_unreachable()` to satisfy the compiler without generating actual return-value code.

## Trailing-Return Zero-Capture Specialization

The zero-capture variant for trailing-return lambdas uses `__builtin_unreachable()` instead of `return 0`. The exact binary text (emitted as two consecutive `a1()` calls):

```cpp
template <typename U, U func, typename Return, unsigned>
struct __nv_dl_trailing_return_tag { };

template <typename U, U func, typename Return, unsigned Id>
struct __nv_dl_wrapper_t<__nv_dl_trailing_return_tag<U, func, Return, Id> > {
  __nv_dl_wrapper_t(__nv_dl_trailing_return_tag<U, func, Return, Id>) { }

  template <typename...U1> Return operator()(U1...) { __builtin_unreachable(); }
};
```

Note: the `__nv_dl_trailing_return_tag` definition and its zero-capture wrapper specialization are emitted together (two strings in immediate succession: the first ends at `{ ` before `__builtin_unreachable`, the second contains `__builtin_unreachable(); }\n}; \n\n` -- note the trailing space before the newlines).

The `__builtin_unreachable()` tells the compiler this code path is never taken, so no return value needs to be materialized. This is safe because the wrapper's `operator()` is never called on the device side -- the device compiler resolves the call through the tag's encoded function pointer directly.

## Per-Capture-Count Specialization Generator (`sub_6BB790`)

The function `sub_6BB790` generates partial specializations of `__nv_dl_wrapper_t` for a specific capture count N. It takes two arguments: the capture count (`unsigned int a1`) and an emit callback (`void(*a2)(const char*)`). For each N, it emits two struct specializations: one for `__nv_dl_tag` and one for `__nv_dl_trailing_return_tag`.

### Generated Template Structure (N captures)

For a lambda capturing N variables, `sub_6BB790(N, emit)` produces:

```cpp
// Standard tag specialization
template <typename Tag, typename F1, typename F2, ..., typename FN>
struct __nv_dl_wrapper_t<Tag, F1, F2, ..., FN> {
    typename __nv_lambda_field_type<F1>::type f1;
    typename __nv_lambda_field_type<F2>::type f2;
    ...
    typename __nv_lambda_field_type<FN>::type fN;

    __nv_dl_wrapper_t(Tag, F1 in1, F2 in2, ..., FN inN)
        : f1(in1), f2(in2), ..., fN(inN) { }

    template <typename...U1>
    int operator()(U1...) { return 0; }
};

// Trailing-return tag specialization
template <typename U, U func, typename Return, unsigned Id,
          typename F1, typename F2, ..., typename FN>
struct __nv_dl_wrapper_t<__nv_dl_trailing_return_tag<U, func, Return, Id>,
                          F1, F2, ..., FN> {
    typename __nv_lambda_field_type<F1>::type f1;
    typename __nv_lambda_field_type<F2>::type f2;
    ...
    typename __nv_lambda_field_type<FN>::type fN;

    __nv_dl_wrapper_t(__nv_dl_trailing_return_tag<U, func, Return, Id>,
                      F1 in1, F2 in2, ..., FN inN)
        : f1(in1), f2(in2), ..., fN(inN) { }

    template <typename...U1>
    Return operator()(U1...) { __builtin_unreachable(); }
};
```

### `__nv_lambda_field_type` Indirection

Each field is declared as `typename __nv_lambda_field_type<Fi>::type fi` rather than `Fi fi`. This indirection allows the lambda infrastructure to intercept array types (which cannot be captured by value in C++) and replace them with `__nv_lambda_array_wrapper` instances that perform element-by-element copying. The primary template is an identity transform:

```cpp
template <typename T>
struct __nv_lambda_field_type {
    typedef T type;
};
```

Specializations for array types (emitted by `sub_6BC290`) map `T[D1]...[DN]` to `__nv_lambda_array_wrapper<T[D1]...[DN]>`, and `const T[D1]...[DN]` to `const __nv_lambda_array_wrapper<T[D1]...[DN]>`.

### Emission Mechanics

The decompiled `sub_6BB790` reveals the emission is entirely printf-based, building C++ source text in a 1064-byte stack buffer (`v29[1064]`) and passing each fragment through the emit callback. The function has two major branches:

**Branch 1: `a1 == 0` (zero captures)** -- Dead code. Falls through to emit `__nv_dl_wrapper_t(Tag,) :` with a trailing comma and empty initializer list, which would produce syntactically invalid C++. This path is never reached because the bitmap scan loop in `sub_6BCC20` skips bit 0 (`if (v2 && (v3 & 1) != 0)`). The zero-capture case is handled by the primary template's `__nv_dl_wrapper_t<Tag>` specialization emitted unconditionally as a string literal in `sub_6BCC20`.

**Branch 2: `a1 > 0` (N captures)** -- Generates the N-ary specializations through seven sequential loops:

```text
Loop 1:  Emit template parameter list    ", typename F1, ..., typename FN"
Loop 2:  Emit partial specialization      ", F1, ..., FN"
Loop 3:  Emit field declarations          "typename __nv_lambda_field_type<Fi>::type fi;\n"
Loop 4:  Emit constructor parameters      "F1 in1, F2 in2, ..., FN inN"
Loop 5:  Emit initializer list            "f1(in1), f2(in2), ..., fN(inN)"
         Emit operator() with "return 0"
         Then repeat Loops 1-5 for __nv_dl_trailing_return_tag variant
Loop 6:  Same parameter/field emission for trailing-return variant
Loop 7:  Same initializer list for trailing-return variant
         Emit operator() with __builtin_unreachable()
```

Each loop uses `sprintf(v29, "...", index)` for numbered parameters and `a2(v29)` to emit the fragment. The first element in each comma-separated list is handled specially (no leading comma), with subsequent elements prefixed by `", "`.

Key string literals used by `sub_6BB790` (extracted from binary):

| String | Purpose |
|---|---|
| `"\ntemplate <typename Tag"` | Opens template parameter list |
| `", typename F%u"` | Each additional type parameter |
| `">\nstruct __nv_dl_wrapper_t<Tag"` | Opens partial specialization |
| `", F%u"` | Each type argument in specialization |
| `"typename __nv_lambda_field_type<F%u>::type f%u;\n"` | Field declaration |
| `"__nv_dl_wrapper_t(Tag,"` | Constructor declaration (standard tag) |
| `"F%u in%u"` | Constructor parameter |
| `"f%u(in%u)"` | Initializer list entry |
| `" { }\ntemplate <typename...U1>\nint operator()(U1...) { return 0; }\n};\n"` | Standard operator() |
| `"__nv_dl_trailing_return_tag<U, func, Return, Id>"` | Trailing-return tag name |
| `" { }\ntemplate <typename...U1>\nReturn operator()(U1...) "` | Trailing-return operator() |
| `"{ __builtin_unreachable(); }\n};\n\n"` | Unreachable body |

### Concrete Example: 2 Captures

For a lambda capturing two variables, `sub_6BB790(2, emit)` produces:

```cpp
template <typename Tag, typename F1, typename F2>
struct __nv_dl_wrapper_t<Tag, F1, F2> {
    typename __nv_lambda_field_type<F1>::type f1;
    typename __nv_lambda_field_type<F2>::type f2;
    __nv_dl_wrapper_t(Tag, F1 in1, F2 in2) : f1(in1), f2(in2) { }
    template <typename...U1>
    int operator()(U1...) { return 0; }
};

template <typename U, U func, typename Return, unsigned Id,
          typename F1, typename F2>
struct __nv_dl_wrapper_t<__nv_dl_trailing_return_tag<U, func, Return, Id>,
                          F1, F2> {
    typename __nv_lambda_field_type<F1>::type f1;
    typename __nv_lambda_field_type<F2>::type f2;
    __nv_dl_wrapper_t(__nv_dl_trailing_return_tag<U, func, Return, Id>,
                      F1 in1, F2 in2) : f1(in1), f2(in2) { }
    template <typename...U1>
    Return operator()(U1...) { __builtin_unreachable(); }
};
```

## Per-Lambda Wrapper Emission (`sub_47B890`)

The backend code generator `sub_47B890` (`gen_lambda` in `cp_gen_be.c`) handles the per-lambda transformation at each lambda expression's usage site. It reads the decision bits at `lambda_info + 25` and emits a wrapper construction call that replaces the lambda expression in the output `.int.c` file.

### Device Lambda Path (bit 3 set: `byte[25] & 0x08`)

When the device lambda flag is set, the emitter produces a wrapper construction expression followed by a `#if 0` block that hides the original lambda body from the host compiler:

```c
// sub_47B890, decompiled lines 46-58
if ((v2 & 8) != 0) {
    sub_467E50("__nv_dl_wrapper_t< ");   // open wrapper type
    sub_475820(a1);                       // emit tag type (closure class)
    sub_46E640(a1);                       // emit capture type list
    sub_467E50(">( ");                    // close template args, open ctor
    sub_475820(a1);                       // emit tag constructor arg
    sub_467E50("{} ");                    // empty-brace tag construction
    sub_46E550(*a1);                      // emit captured value expressions
    sub_467E50(") ");                     // close ctor call
    sub_46BC80("#if 0");                  // suppress original lambda
    --dword_1065834;                      // adjust nesting depth
    sub_467D60();                         // newline
}
```

The generated output for a device lambda with two captures looks like:

```cpp
__nv_dl_wrapper_t< __nv_dl_tag<decltype(&ClosureType::operator()),
    &ClosureType::operator(), 0u>, int, float>(
    __nv_dl_tag<decltype(&ClosureType::operator()),
    &ClosureType::operator(), 0u>{}, x, y)
#if 0
// original lambda body hidden from host compiler
[x, y]() __device__ { /* ... */ }
#endif
```

The `#if 0` suppression ensures the host compiler never attempts to parse the device lambda body, which may contain device-only intrinsics and constructs. The device compiler sees the wrapper struct and resolves the call through the tag type's encoded function pointer.

### Body Suppression for Host-Only Pass (bit pattern `byte[25] & 0x06 == 0x02`)

A separate suppression path handles lambdas where the body should not be compiled on the current pass. In this case, the emitter outputs an empty body `{ }` and wraps the real body in `#if 0` / `#endif`:

```c
// sub_47B890, decompiled lines 290-306
if ((*(_BYTE *)(a1 + 25) & 6) == 2) {
    sub_467D60();             // newline
    sub_468190("{ }");        // empty body placeholder
    sub_46BC80("#if 0");      // start suppression
    --dword_1065834;
    sub_467D60();
}
// ... emit original body under #if 0 ...
sub_47AEF0(body, 0);         // emit body (invisible due to #if 0)
if ((*(_BYTE *)(a1 + 25) & 6) == 2) {
    sub_46BC80("#endif");     // end suppression
    --dword_1065834;
    sub_467D60();
    dword_1065820 = 0;
    qword_1065828 = 0;
}
```

After the body emission completes, the device lambda path also emits a matching `#endif` to close the `#if 0` block opened at the wrapper call:

```c
// sub_47B890, decompiled lines 312-320
if ((v29 & 8) != 0) {          // device lambda
    sub_46BC80("#endif");       // close #if 0 from wrapper call
    --dword_1065834;
    sub_467D60();
    dword_1065820 = 0;
    qword_1065828 = 0;
}
```

### Host-Device Lambda Path (bit 4 set: `byte[25] & 0x10`)

Host-device lambdas take a different path through `__nv_hdl_create_wrapper_t` rather than `__nv_dl_wrapper_t`. This is covered in the [Host-Device Lambda Wrapper](./host-device-wrapper.md) page.

## Bitmap-Driven Emission

Only capture counts that were actually used during frontend parsing get specializations emitted. The scan loop in `sub_6BCC20` processes the 128-byte bitmap at `unk_1286980` as an array of 16 `uint64_t` values:

```c
uint64_t *ptr = (uint64_t *)&unk_1286980;
unsigned int idx = 0;
do {
    uint64_t word = *ptr;
    unsigned int limit = idx + 64;
    do {
        if (idx != 0 && (word & 1))       // skip bit 0 (handled by primary)
            sub_6BB790(idx, callback);     // emit N-capture specialization
        ++idx;
        word >>= 1;
    } while (limit != idx);
    ++ptr;
} while (limit != 1024);
```

Bit 0 is skipped because the zero-capture case is already handled by the primary template's `__nv_dl_wrapper_t<Tag>` specialization (emitted unconditionally as a string literal). For each remaining set bit N, `sub_6BB790(N, emit)` produces two structs (standard tag and trailing-return tag), meaning a translation unit using lambdas with 1, 3, and 5 captures emits exactly 6 wrapper struct specializations rather than the full 2048 that exhaustive generation would produce.

## Detection Traits

After all wrapper specializations are emitted, `sub_6BCC20` emits SFINAE trait templates that allow compile-time detection of device-lambda wrapper types. These are emitted AFTER the host-device wrapper infrastructure (steps 7-12 in the emission sequence), not immediately after the device bitmap scan. Each trait + its `#define` macro is emitted as a single `a1()` call:

```cpp
// Emitted as one string (step 13 in sub_6BCC20):
template <typename T>
struct __nv_extended_device_lambda_trait_helper {
  static const bool value = false;
};
template <typename T1, typename...Pack>
struct __nv_extended_device_lambda_trait_helper<__nv_dl_wrapper_t<T1, Pack...> > {
  static const bool value = true;
};
#define __nv_is_extended_device_lambda_closure_type(X) __nv_extended_device_lambda_trait_helper< typename __nv_lambda_trait_remove_cv<X>::type>::value
```

Note: in the binary, the `#define` is a single line (no backslash continuation). The 2-space indentation on `static const bool` matches the binary exactly.

An unwrapper trait strips the wrapper to recover the inner tag type (step 14 in emission):

```cpp
template<typename T> struct __nv_lambda_trait_remove_dl_wrapper { typedef T type; };
template<typename T> struct __nv_lambda_trait_remove_dl_wrapper< __nv_dl_wrapper_t<T> > { typedef T type; };
```

A separate trait detects whether a wrapper uses a trailing-return tag (step 15 in emission):

```cpp
template <typename T>
struct __nv_extended_device_lambda_with_trailing_return_trait_helper {
  static const bool value = false;
};
template <typename U, U func, typename Return, unsigned Id, typename...Pack>
struct __nv_extended_device_lambda_with_trailing_return_trait_helper<__nv_dl_wrapper_t<__nv_dl_trailing_return_tag<U, func, Return, Id>, Pack...> > {
  static const bool value = true;
};
#define __nv_is_extended_device_lambda_with_preserved_return_type(X) __nv_extended_device_lambda_with_trailing_return_trait_helper< typename __nv_lambda_trait_remove_cv<X>::type >::value
```

Note: the emission order in `sub_6BCC20` is: device trait (step 13), then `__nv_lambda_trait_remove_dl_wrapper` (step 14), then trailing-return trait (step 15), then host-device trait (step 16). The unwrapper appears between the two detection traits, not after both of them.

These traits and macros enable the CUDA runtime headers and device compiler to distinguish wrapped device lambdas from ordinary closure types at compile time, which is necessary for proper template argument deduction in kernel launch expressions.

## Function Map

| Address | Name (recovered) | Source | Lines | Role |
|---|---|---|---|---|
| `sub_6BB790` | `emit_device_lambda_wrapper_specialization` | `nv_transforms.c` | 191 | Emit `__nv_dl_wrapper_t<Tag, F1..FN>` for N captures (both tag variants) |
| `sub_6BCC20` | `nv_emit_lambda_preamble` | `nv_transforms.c` | 244 | Master emitter: primary template, zero-capture, bitmap scan, traits |
| `sub_6BCBF0` | `nv_record_capture_count` | `nv_transforms.c` | 13 | Set bit N in device or host-device bitmap |
| `sub_6BCBC0` | `nv_reset_capture_bitmasks` | `nv_transforms.c` | 9 | Zero both 128-byte bitmaps before each TU |
| `sub_47B890` | `gen_lambda` | `cp_gen_be.c` | 336 | Per-lambda wrapper call emission in `.int.c` output |
| `sub_467E50` | `emit_string` | `cp_gen_be.c` | -- | Low-level string emitter to output buffer |
| `sub_46BC80` | `emit_preprocessor_directive` | `cp_gen_be.c` | -- | Emit `#if 0` / `#endif` suppression blocks |
| `sub_475820` | `emit_closure_tag_type` | `cp_gen_be.c` | -- | Emit tag type for wrapper construction |
| `sub_46E640` | `emit_capture_type_list` | `cp_gen_be.c` | -- | Emit template argument list of capture types |
| `sub_46E550` | `emit_capture_value_list` | `cp_gen_be.c` | -- | Emit constructor arguments (captured values) |
| `sub_6BC290` | `emit_array_capture_helpers` | `nv_transforms.c` | 183 | Emit `__nv_lambda_array_wrapper` for dim 2-8 |

## Global State

| Variable | Address | Purpose |
|---|---|---|
| `unk_1286980` | `0x1286980` | Device lambda capture-count bitmap (128 bytes, 1024 bits) |
| `dword_106BF38` | `0x106BF38` | `--extended-lambda` mode flag (enables entire system) |
| `dword_1065834` | `0x1065834` | Preprocessor nesting depth (decremented on `#if 0` emission) |
| `dword_1065820` | `0x1065820` | Output state flag (reset after `#endif` emission) |
| `qword_1065828` | `0x1065828` | Output state pointer (reset after `#endif` emission) |

## Related Pages

- [Extended Lambda Overview](./overview.md) -- End-to-end flow through the five pipeline stages
- [Host-Device Lambda Wrapper](./host-device-wrapper.md) -- `__nv_hdl_wrapper_t` type-erased design
- [Capture Handling](./capture-handling.md) -- `__nv_lambda_field_type`, `__nv_lambda_array_wrapper` for array captures
- [Preamble Injection](./preamble-injection.md) -- `sub_6BCC20` full emission sequence
- [Lambda Restrictions](./restrictions.md) -- Validation rules and error codes
- [Kernel Stub Generation](../cuda/kernel-stubs.md) -- Parallel `#if 0` suppression pattern for `__global__` functions
